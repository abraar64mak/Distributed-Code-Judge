import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runner_unsupported_language(monkeypatch):
    runner = _load("runner", "services/sandbox-runner/runner.py")
    monkeypatch.setattr(runner.docker, "from_env", lambda: object())  # no Docker daemon needed
    assert runner.run_in_sandbox("brainfuck", "+")["status"] == "failed"


def test_api_health_route_registered():
    api = _load("api_main", "services/api/main.py")
    paths = {r.path for r in api.app.routes}
    assert {"/health", "/submissions", "/leaderboard/{problem_id}"} <= paths
