"""Grading logic, exercised with a fake sandbox so no Docker daemon is needed."""

import pytest
from conftest import load_module

runner = load_module("runner", "services/sandbox-runner/runner.py")


def fake(outputs):
    """Returns a run function that replays `outputs` (stdout, exit_code, timed_out) in order."""
    it = iter(outputs)

    def run(language, code, stdin, timeout):
        stdout, exit_code, timed_out = next(it)
        return {"exit_code": exit_code, "stdout": stdout, "stderr": "", "timed_out": timed_out, "ms": 5}

    return run


CASES = [{"id": 1, "stdin": "", "expected": "3\n"}, {"id": 2, "stdin": "", "expected": "0\n"}]


def test_all_correct():
    r = runner.judge("python", "x", CASES, run=fake([("3\n", 0, False), ("0", 0, False)]))
    assert (r["status"], r["score"]) == ("completed", 100.0)


def test_partial_credit_and_whitespace_tolerance():
    r = runner.judge("python", "x", CASES, run=fake([("3  \n\n", 0, False), ("1\n", 0, False)]))
    assert (r["status"], r["score"]) == ("completed", 50.0)
    assert [c["passed"] for c in r["cases"]] == [True, False]


def test_runtime_error_fails():
    r = runner.judge("python", "x", CASES, run=fake([("", 1, False), ("0\n", 0, False)]))
    assert r["status"] == "failed" and r["score"] == 50.0


def test_timeout_wins():
    r = runner.judge("python", "x", CASES, run=fake([("", -1, True), ("", 1, False)]))
    assert r["status"] == "timed_out"


def test_ungraded_run_when_no_cases():
    r = runner.judge("python", "x", [], run=fake([("anything", 0, False)]))
    assert (r["status"], r["score"]) == ("completed", 100.0)


def test_unsupported_language():
    assert runner.judge("cobol", "x", CASES)["status"] == "failed"


@pytest.mark.parametrize("a,b", [("a \r\nb\n\n", "a\nb"), ("", "")])
def test_normalize(a, b):
    assert runner.normalize(a) == b


def test_shell_commands_never_interpolate_user_code():
    for cmd in runner.SHELL_COMMANDS.values():
        assert "{" not in cmd and "$JUDGE_CODE" in cmd
