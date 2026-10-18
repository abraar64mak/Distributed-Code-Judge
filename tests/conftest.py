import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_module(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # pydantic resolves postponed annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


def docker_ready(images=None) -> bool:
    """True if a Docker daemon is reachable and the sandbox images are already present locally."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        runner = load_module("runner_for_probe", "services/sandbox-runner/runner.py")
        for image in images or set(runner.LANGUAGE_IMAGES.values()):
            client.images.get(image)
        return True
    except Exception:
        return False
