"""Policy proxy tests: pure rule checks, plus live checks against a real Docker daemon (skipped without one)."""

import copy
import json
import threading
import urllib.error
import urllib.request

import pytest
from conftest import docker_ready, load_module

proxy = load_module("docker_proxy", "services/docker-proxy/proxy.py")

GOOD = {
    "User": "65534:65534",
    "Tty": False,
    "OpenStdin": False,
    "StdinOnce": False,
    "AttachStdin": False,
    "AttachStdout": False,
    "AttachStderr": False,
    "Env": ["JUDGE_CODE=print(1)", "JUDGE_INPUT="],
    "Cmd": ["sh", "-c", "true"],
    "Image": "python:3.11-slim",
    "NetworkDisabled": True,
    "WorkingDir": "/tmp",
    "Labels": {"judge.sandbox": "1"},
    "HostConfig": {
        "Memory": 268435456,
        "MemorySwap": 268435456,
        "ReadonlyRootfs": True,
        "NetworkMode": "default",
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges"],
        "Tmpfs": {"/tmp": "size=64m,mode=1777,exec"},
        "PidsLimit": 64,
        "NanoCpus": 1000000000,
    },
}


def mutated(**changes):
    spec = copy.deepcopy(GOOD)
    for key, value in changes.items():
        target = spec["HostConfig"] if key.startswith("hc_") else spec
        name = key.removeprefix("hc_")
        if value is ...:
            target.pop(name, None)
        else:
            target[name] = value
    return spec


def test_good_spec_is_allowed():
    proxy.check_create(GOOD)


@pytest.mark.parametrize(
    "spec",
    [
        mutated(hc_Privileged=True),
        mutated(hc_Binds=["/:/host"]),
        mutated(hc_Mounts=[{"Type": "bind", "Source": "/", "Target": "/h"}]),
        mutated(hc_NetworkMode="host"),
        mutated(hc_PidMode="host"),
        mutated(hc_CapAdd=["SYS_ADMIN"]),
        mutated(hc_CapDrop=["NET_RAW"]),
        mutated(hc_CapDrop=...),
        mutated(hc_SecurityOpt=["seccomp=unconfined"]),
        mutated(hc_SecurityOpt=...),
        mutated(hc_ReadonlyRootfs=False),
        mutated(hc_Memory=64 * 1024**3, hc_MemorySwap=64 * 1024**3),
        mutated(hc_MemorySwap=-1),
        mutated(hc_PidsLimit=0),
        mutated(hc_PidsLimit=100000),
        mutated(hc_NanoCpus=64 * 10**9),
        mutated(hc_Tmpfs={"/etc": "rw"}),
        mutated(hc_Runtime="nvidia"),
        mutated(hc_Devices=[{"PathOnHost": "/dev/sda"}]),
        mutated(User="root"),
        mutated(User=...),
        mutated(NetworkDisabled=False),
        mutated(Image="alpine"),
        mutated(Image="python:latest"),
        mutated(WorkingDir="/"),
        mutated(Labels={}),
        mutated(Volumes={"/data": {}}),
        mutated(Entrypoint=["/bin/sh"]),
        mutated(HostConfig=...),
        [],
    ],
)
def test_dangerous_specs_are_denied(spec):
    with pytest.raises(proxy.PolicyError):
        proxy.check_create(spec)


def test_json_tricks_cannot_smuggle_fields():
    """Docker's Go JSON decoder matches field names case-insensitively and decodes \\u escapes, so the allow-list
    must reject anything that is not an exact, known key rather than only blacklisting dangerous names."""
    for raw in [
        '{"privileged": true}',  # wrong case, Go would still accept it
        '{"Priv\\u0069leged": true}',  # escape spelling of Privileged
        '{"binds": ["/:/h"]}',
        '{"BINDS": ["/:/h"]}',
    ]:
        spec = copy.deepcopy(GOOD)
        spec["HostConfig"].update(json.loads(raw))
        with pytest.raises(proxy.PolicyError):
            proxy.check_create(spec)
    spec = copy.deepcopy(GOOD)
    spec.update(json.loads('{"user": "root", "hostconfig": {"Privileged": true}}'))
    with pytest.raises(proxy.PolicyError):
        proxy.check_create(spec)
    # duplicate exact keys: both Python and Go keep the last value, so the value we validate is the one Docker sees
    dup = json.loads('{"User": "root", "User": "65534:65534"}')
    assert dup["User"] == "65534:65534"


def test_runner_request_matches_policy():
    """What the runner really sends (captured from docker-py) must satisfy the policy."""
    runner = load_module("runner", "services/sandbox-runner/runner.py")
    assert runner.MEMORY_LIMITS["java"] == "512m"
    assert runner.PIDS_LIMIT <= proxy.MAX_PIDS
    assert runner.SANDBOX_RUNTIME in (None, *proxy.ALLOWED_RUNTIMES)
    assert set(runner.LANGUAGE_IMAGES.values()) == set(proxy.ALLOWED_IMAGES)


# --------------------------------------------------------------------------- live Docker


live = pytest.mark.skipif(not docker_ready(), reason="Docker daemon or sandbox images unavailable")


@pytest.fixture
def proxied(monkeypatch):
    server = proxy.serve("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("DOCKER_HOST", url.replace("http://", "tcp://"))
    yield url
    server.shutdown()


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, method=method, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


@live
def test_runner_works_through_the_proxy(proxied):
    runner = load_module("runner", "services/sandbox-runner/runner.py")
    result = runner.run_case("python", "print(int(input()) * 2)", "21\n", 20)
    assert result.get("infra_error") is None, result
    assert (result["exit_code"], result["stdout"].strip()) == (0, "42")
    slow = runner.run_case("python", "while True: pass", "", 2)
    assert slow["timed_out"] is True  # exercises wait-timeout -> kill -> logs -> remove through the proxy


@live
def test_privileged_and_bind_mount_requests_are_refused(proxied):
    status, body = call(proxied, "POST", "/v1.50/containers/create", mutated(hc_Privileged=True))
    assert status == 403 and "not allowed" in body
    status, _ = call(proxied, "POST", "/containers/create", mutated(hc_Binds=["/:/host"]))
    assert status == 403


@live
def test_everything_else_is_refused(proxied):
    for method, path in [
        ("GET", "/containers/json"),
        ("GET", "/volumes"),
        ("GET", "/info"),
        ("POST", "/containers/abcdef123456/exec"),
        ("POST", "/build"),
        ("POST", "/images/create?fromImage=alpine&tag=latest"),
        ("GET", "/networks"),
    ]:
        body = {} if method == "POST" else None
        assert call(proxied, method, path, body)[0] == 403, path
    assert call(proxied, "GET", "/_ping")[0] == 200


@live
def test_cannot_touch_containers_it_did_not_create(proxied):
    import docker

    victim = docker.DockerClient(base_url="unix:///var/run/docker.sock").containers.run(
        "python:3.11-slim", ["sleep", "30"], detach=True
    )
    try:
        for method, path in [("POST", f"/containers/{victim.id}/kill"), ("DELETE", f"/containers/{victim.id}?force=1")]:
            status, body = call(proxied, method, path, {} if method == "POST" else None)
            assert status == 403 and "not a sandbox container" in body, (method, status, body)
        victim.reload()
        assert victim.status == "running"
    finally:
        victim.remove(force=True)
