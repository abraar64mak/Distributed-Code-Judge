from conftest import load_module


def test_api_routes_registered():
    api = load_module("api_main", "services/api/main.py")
    paths = {r.path for r in api.app.routes}
    assert {
        "/health",
        "/problems",
        "/problems/{problem_id}",
        "/submissions",
        "/submissions/{submission_id}",
        "/submissions/{submission_id}/results",
        "/users/{user_id}/submissions",
        "/leaderboard/{problem_id}",
    } <= paths
