"""Runs every sample solution through the real grading logic, using local interpreters instead of Docker.

Skipped per-language when the interpreter is missing. Proves the examples and the grader agree; it does not
exercise the Docker sandbox (use scripts/try_examples.py against a running stack for that).
"""

import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest
from conftest import ROOT, load_module

runner = load_module("runner", "services/sandbox-runner/runner.py")
EXAMPLES = ROOT / "examples"
MANIFEST = json.loads((EXAMPLES / "manifest.json").read_text())

SEEDED_SUM_TWO = {
    "id": "sum-two",
    "test_cases": [
        {"stdin": "1 2\n", "expected_stdout": "3\n"},
        {"stdin": "-5 5\n", "expected_stdout": "0\n"},
        {"stdin": "1000000 2000000\n", "expected_stdout": "3000000\n"},
    ],
}
TOOLS = {"python": "python3", "javascript": "node", "java": "javac"}


def local_run(language, code, stdin, timeout):
    """Stand-in for run_case: same contract, subprocess instead of a container."""
    with tempfile.TemporaryDirectory() as d:
        try:
            if language == "java":
                pathlib.Path(d, "Main.java").write_text(code)
                compiled = subprocess.run(["javac", "Main.java"], cwd=d, capture_output=True, text=True, timeout=60)
                if compiled.returncode != 0:
                    return {"exit_code": 1, "stdout": "", "stderr": compiled.stderr, "timed_out": False, "ms": 0}
                cmd = ["java", "-Xmx128m", "Main"]
            elif language == "python":
                cmd = ["python3", "-B", "-c", code]
            else:
                cmd = ["node", "-e", code]
            p = subprocess.run(cmd, cwd=d, input=stdin, capture_output=True, text=True, timeout=timeout)
            return {"exit_code": p.returncode, "stdout": p.stdout, "stderr": p.stderr, "timed_out": False, "ms": 1}
        except subprocess.TimeoutExpired:
            return {"exit_code": -1, "stdout": "", "stderr": "", "timed_out": True, "ms": timeout * 1000}


def problem_cases(problem_id):
    if problem_id == "sum-two":
        data = SEEDED_SUM_TWO
    else:
        data = json.loads((EXAMPLES / "problems" / f"{problem_id}.json").read_text())
    return [
        {"id": i, "stdin": tc["stdin"], "expected": tc["expected_stdout"]}
        for i, tc in enumerate(data["test_cases"], start=1)
    ]


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["file"])
def test_example_solution(entry):
    if shutil.which(TOOLS[entry["language"]]) is None:
        pytest.skip(f"{TOOLS[entry['language']]} not installed")
    code = (EXAMPLES / "solutions" / entry["file"]).read_text()
    result = runner.judge(entry["language"], code, problem_cases(entry["problem"]), timeout=2, run=local_run)
    assert (result["status"], result["score"]) == (entry["status"], entry["score"])


def test_seeded_sum_two_matches_seed_sql():
    seed = (ROOT / "db" / "seed.sql").read_text()
    for tc in SEEDED_SUM_TWO["test_cases"]:
        assert tc["stdin"].strip().replace("\n", "") in seed.replace("\\n", "")
