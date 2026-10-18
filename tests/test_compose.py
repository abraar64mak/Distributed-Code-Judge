"""Guards the sandbox deployment posture so a refactor can't silently re-expose the Docker socket."""

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]


def test_runner_has_no_direct_docker_socket():
    runner = compose["sandbox-runner"]
    assert not any("docker.sock" in v for v in runner.get("volumes", []))
    assert runner["environment"]["DOCKER_HOST"] == "tcp://docker-proxy:2375"


def test_only_proxy_mounts_socket_read_only_and_is_unpublished():
    mounts = [(name, v) for name, svc in compose.items() for v in svc.get("volumes", []) if "docker.sock" in v]
    assert mounts == [("docker-proxy", "/var/run/docker.sock:/var/run/docker.sock:ro")]
    assert "ports" not in compose["docker-proxy"]


def test_proxy_is_the_policy_proxy_built_from_this_repo():
    proxy = compose["docker-proxy"]
    assert proxy["build"]["dockerfile"] == "services/docker-proxy/Dockerfile"
    assert "image" not in proxy  # no generic third-party endpoint filter


def test_postgres_published_on_localhost_only():
    assert all(p.startswith("127.0.0.1:") for p in compose["postgres"]["ports"])
