"""The real sandbox limits, exercised through run_case in real containers (skipped without Docker + images)."""

import pytest
from conftest import docker_ready, load_module

pytestmark = pytest.mark.skipif(not docker_ready(), reason="Docker daemon or sandbox images unavailable")

runner = load_module("runner", "services/sandbox-runner/runner.py")


def run(language, code, stdin="", timeout=30):
    result = runner.run_case(language, code, stdin, timeout)
    assert not result.get("infra_error"), result
    return result


def test_stdin_and_languages():
    assert run("python", "print(int(input()) * 2)", "21\n")["stdout"].strip() == "42"
    js = "console.log(Number(require('fs').readFileSync(0,'utf8')) * 2)"
    assert run("javascript", js, "21\n")["stdout"].strip() == "42"
    java = 'public class Main{public static void main(String[] a){System.out.println("ok");}}'
    assert run("java", java, timeout=90)["stdout"].strip() == "ok"


def test_runs_unprivileged():
    code = (
        "import os\n"
        "print(os.getuid())\n"
        "print(*[l for l in open('/proc/self/status') if l.startswith(('CapEff:', 'NoNewPrivs:'))])\n"
    )
    out = run("python", code)
    assert out["stdout"].splitlines()[0] == "65534"
    assert "CapEff:\t0000000000000000" in out["stdout"] and "NoNewPrivs:\t1" in out["stdout"]


def test_no_network():
    r = run("python", "import socket;s=socket.socket();s.settimeout(3);s.connect(('1.1.1.1',80))")
    assert r["exit_code"] != 0 and "unreachable" in r["stderr"].lower()


def test_filesystem_is_read_only_except_tmp():
    assert run("python", "open('/x','w')")["exit_code"] != 0
    assert run("python", "open('/tmp/x','w').write('1');print('ok')")["stdout"].strip() == "ok"


def test_memory_limit_kills_hog():
    assert run("python", "x = bytearray(10**9); print('allocated')")["exit_code"] == 137


def test_pid_limit_stops_fork_bomb():
    code = (
        "import os,time\n"
        "for i in range(500):\n"
        "    try:\n"
        "        if os.fork() == 0:\n"
        "            time.sleep(3); os._exit(0)\n"
        "    except OSError:\n"
        "        print('blocked', i); break\n"
        "else:\n"
        "    print('FORKED ALL')\n"
    )
    out = run("python", code)["stdout"]
    assert out.startswith("blocked") and int(out.split()[1]) <= runner.PIDS_LIMIT


def test_timeout_kills_and_cleans_up():
    import docker

    before = {c.id for c in docker.from_env().containers.list(all=True)}
    assert runner.run_case("python", "while True: pass", "", 2)["timed_out"] is True
    after = {c.id for c in docker.from_env().containers.list(all=True)}
    assert after == before  # container force-removed


def test_output_is_truncated():
    out = run("python", "print('A' * 500000)")["stdout"]
    assert len(out) <= runner.MAX_OUTPUT_BYTES + 40 and out.endswith("[output truncated]")


def test_source_is_never_interpreted_by_a_shell():
    out = run("python", "print('$(id) `id` \"q\" \\'s\\'')")["stdout"].strip()
    assert out == "$(id) `id` \"q\" 's'"


def test_compile_error_is_reported_as_failure():
    r = run("java", "public class Main{ void x( }", timeout=90)
    assert r["exit_code"] != 0 and "error" in r["stderr"]
