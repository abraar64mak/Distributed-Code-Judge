# Distributed Code Judge: architecture walkthrough

A guided tour for a secure-computing audience: how a submission flows through the system, why it is built this
way, what protects the host from hostile code, and where it is still weak. Companion docs:
[`architecture.md`](architecture.md) (component reference) and [`SETUP.md`](SETUP.md) (running it, configuration).

## 1. The problem

A judge runs code written by strangers. Everything interesting follows from that one fact: the submitter controls
the program, its inputs, its resource use and its output, and wants to escape, exhaust the host, read other
people's data, or cheat the score. The design goal is that **a malicious submission can at worst waste its own
time budget and get a bad score.**

## 2. The flow

```
 client ──POST /submissions──▶ API ──insert(queued)──▶ PostgreSQL
                                │                          ▲
                                └──publish (acked)──▶ Kafka │
                                                      │     │ running / results / score
                                                      ▼     │
                                                  runner ───┘
                                                      │ create / start / wait / kill / remove
                                                      ▼
                                            docker-socket-proxy ──▶ Docker daemon
                                                                         │ one container per test case
                                                                         ▼
                                                                  sandbox (no network, no caps, non-root)
```

1. **Submit.** The API validates (language allow-list, size cap, problem exists, per-user rate limit, optional API
   key), writes the submission as `queued` plus an audit row, then publishes to Kafka and waits for the broker's
   acknowledgement. If it can't enqueue, the submission is marked `failed` and the client gets 503; nothing is
   left stuck in `queued`.
2. **Dispatch.** Kafka decouples intake from execution. Bursts queue up instead of blocking the API, and runners
   scale horizontally via one consumer group.
3. **Judge.** The runner marks the submission `running`, loads the time limit and test cases **straight from
   PostgreSQL**, and runs the code once per test case in a fresh container. Expected outputs never travel through
   Kafka or the API, so a submitter can't read them.
4. **Grade.** Stdout is compared with the expected output ignoring trailing whitespace. Score = percentage of
   cases passed. Status is `timed_out` if any case timed out, `failed` on a crash or compile error, else
   `completed`.
5. **Persist.** One transaction writes status, score, per-case results, an audit row and the leaderboard update.
   Only after that commits does the runner commit the Kafka offset.

## 3. Reliability design

| Concern | Mechanism |
|---|---|
| Runner crashes mid-job | Offset is committed only after results are stored, so Kafka redelivers (at-least-once). |
| Redelivered message | `persist_result` and `mark_running` lock the row (`FOR UPDATE`) and no-op if the submission is already finished, so redelivery is idempotent. |
| Judging code throws | `handle_message` catches it and records `failed`; every submission reaches a terminal state. |
| Broker down at submit | Submission marked `failed`, client sees 503. |
| Audit integrity | Every transition (`queued → running → terminal`) is an append-only row written in the same transaction as the change. |
| Leaderboard speed | `leaderboard_cache`, updated incrementally and indexed on `(problem_id, best_score DESC, achieved_at)`, so reads never scan `submissions`. |

## 4. Threat model

**Assets:** the host machine and Docker daemon, the database (including other users' code and the hidden test
cases), the grading integrity, and availability for other users.

**Attacker:** an authenticated-or-not API client who submits arbitrary code in a supported language.

### What the sandbox enforces (per test case, per container)

| Control | Setting | Mitigates |
|---|---|---|
| No network | `network_disabled` | exfiltration, calling internal services, DB/Kafka access, scanning |
| Memory + swap cap | 256 MB (512 MB Java), swap = memory | memory exhaustion, OOM of the host |
| CPU cap | 1 CPU | CPU starvation of other jobs |
| PID limit | 64 | fork bombs |
| Capabilities | `cap_drop: ALL` | raw sockets, mounting, `chown`, ptrace, etc. |
| Privilege escalation | `no-new-privileges` | setuid binaries |
| User | 65534 (nobody) | root-owned file access inside the container |
| Filesystem | read-only root, 64 MB tmpfs `/tmp` | persistent tampering, disk fill |
| Time | wall-clock timeout, then `kill` | infinite loops, sleeping |
| Output | truncated at 64 KB | log flooding, memory blow-up in the runner |
| Input to the shell | code/stdin passed as **environment variables**, never interpolated | command injection through source text |
| Optional runtime | `SANDBOX_RUNTIME=runsc` (gVisor) | kernel-level escapes by shrinking the syscall surface |

Each container is created fresh and force-removed afterwards, so no state carries between submissions.

### Platform controls

- The runner does **not** mount the Docker socket. It talks to `docker-socket-proxy`, which holds the socket
  read-only, is not published to the host, and allows only container and image calls. Exec, volumes, networks,
  swarm, build and system endpoints return 403.
- Postgres is bound to `127.0.0.1`; its password is configurable.
- The API has a write key (`API_KEY`), optional read protection (`PROTECT_READS`) and a per-user rate limit.
- A test (`tests/test_compose.py`) fails if a future change re-exposes the socket or publishes the proxy.

## 5. Known weaknesses (discussion material)

1. **The socket proxy filters endpoints, not parameters.** An attacker who gains control of the *runner process*
   (not the sandbox) could ask the proxy to `POST /containers/create` with `Privileged: true` or a host bind
   mount. The runner is the trusted component here; compromising it, for example through a bug in how it parses
   Kafka messages, defeats the proxy. Fixes: a policy-enforcing Docker authorization plugin (OPA), a rootless
   Docker daemon dedicated to sandboxes, or microVMs (Firecracker, Kata) on a separate host.
2. **Shared kernel.** Containers share the host kernel; a kernel exploit escapes. gVisor/Kata reduce this; a
   dedicated machine contains the blast radius.
3. **No custom seccomp/AppArmor profile**, only Docker's default.
4. **Side channels and timing.** Co-located jobs can measure each other (cache timing, CPU contention).
   Runtimes reported to users are wall-clock and noisy.
5. **Source in the environment.** Code travels as env vars, so it appears in `docker inspect`, and very large
   submissions are capped (64,000 characters) for this reason.
6. **Auth is a single shared key.** There are no per-user identities, so `user_id` is self-asserted: anyone can
   submit as anyone, and the rate limit keys on that same self-asserted value. Reads are open unless
   `PROTECT_READS` is set.
7. **Rate limiting is in-memory per API process**, so it resets on restart and isn't shared across replicas.
8. **Judge fairness:** comparing stdout only. Hard-coding the visible sample outputs passes the sample cases,
   so use hidden cases; the `PUT /problems` API supports many.
9. **Defaults:** `judge/judge` credentials and no TLS. Fine for a lab laptop, not for exposure.

## 6. Experiments to run

Run the stack (`make up`), then:

- **Resource limits:** submit `while True: pass` (expect `timed_out`), a memory hog such as
  `x = bytearray(10**9)` (expect `failed`, killed by the cap), and `import os; [os.fork() for _ in range(1000)]`
  (the PID limit should stop it).
- **Network:** submit Python that opens a socket to `1.1.1.1:80`; it should fail immediately.
- **Filesystem:** try writing to `/` (read-only error) versus `/tmp` (works, vanishes afterwards).
- **Privilege:** print `os.getuid()` (65534) and read `/proc/self/status` to see `CapEff: 0000000000000000`.
- **Injection:** submit source containing quotes, `$(id)` and backticks; it should run as plain text.
- **Docker access:** from inside a submission you can't reach the daemon. From the *runner* container, try
  `curl docker-proxy:2375/containers/json` (allowed) versus `/volumes` or `/exec` (denied) to see the proxy's
  endpoint filtering.
- **Hardening exercise:** try creating a privileged container through the proxy and observe that it is permitted.
  Then close the gap: add an OPA authorization plugin, or switch `SANDBOX_RUNTIME=runsc` and compare behaviour.
- **Reliability:** `docker compose kill sandbox-runner` mid-run, restart it, and confirm the submission still
  reaches a terminal state exactly once (check `/submissions/{id}/audit`).

## 7. Where to look in the code

| Topic | File |
|---|---|
| Sandbox options, grading, idempotent persistence | `services/sandbox-runner/runner.py` |
| API, validation, auth, rate limit, enqueue handling | `services/api/main.py` |
| Schema, indexes, audit and results tables | `db/schema.sql` |
| Deployment posture (proxy, ports, runtime) | `docker-compose.yml` |
| Posture regression tests | `tests/test_compose.py` |
| End-to-end flow tests | `tests/test_integration.py` |
| Sample problems and solutions in three languages | `examples/`, `scripts/try_examples.py` |
