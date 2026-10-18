"""
Policy-enforcing Docker API proxy for the sandbox runner (standard library only).

The runner never talks to the Docker socket directly. This proxy sits in front of it and, unlike an
endpoint-only filter, inspects the *parameters* of container creation, so a compromised runner cannot ask
for a privileged container, a host bind mount, host networking, extra capabilities, a different user, an
arbitrary image, or unbounded resources. It also only lets the runner touch containers it created itself
(identified by a label), so the runner cannot kill or remove Postgres, Kafka or anything else.

Everything not explicitly allowed is refused with HTTP 403.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "2375"))

ALLOWED_IMAGES = set(
    filter(
        None,
        os.environ.get(
            "JUDGE_ALLOWED_IMAGES", "python:3.11-slim,node:20-slim,eclipse-temurin:21-jdk"
        ).split(","),
    )
)
ALLOWED_RUNTIMES = set(os.environ.get("JUDGE_ALLOWED_RUNTIMES", "runc,runsc").split(","))

SANDBOX_LABEL = "judge.sandbox"
SANDBOX_USER = "65534:65534"
MAX_MEMORY = 1024 * 1024 * 1024
MAX_PIDS = 256
MAX_NANO_CPUS = 2_000_000_000
MAX_BODY = 1024 * 1024

TOP_LEVEL_KEYS = {
    "User", "Tty", "OpenStdin", "StdinOnce", "AttachStdin", "AttachStdout", "AttachStderr",
    "Env", "Cmd", "Image", "NetworkDisabled", "WorkingDir", "HostConfig", "Labels",
}  # fmt: skip
HOST_CONFIG_KEYS = {
    "Memory", "MemorySwap", "ReadonlyRootfs", "NetworkMode", "CapDrop", "SecurityOpt", "Tmpfs",
    "PidsLimit", "NanoCpus", "Runtime",
}  # fmt: skip

VERSION_PREFIX = re.compile(r"^/v[0-9]+\.[0-9]+")
CONTAINER_ID = r"[0-9a-f]{12,64}"
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "content-length"}


class PolicyError(Exception):
    pass


def check_create(spec: dict) -> None:
    """Raise PolicyError unless `spec` (the JSON body of POST /containers/create) is an acceptable sandbox."""
    if not isinstance(spec, dict):
        raise PolicyError("body must be a JSON object")
    extra = set(spec) - TOP_LEVEL_KEYS
    if extra:
        raise PolicyError(f"field(s) not allowed: {sorted(extra)}")
    if spec.get("Image") not in ALLOWED_IMAGES:
        raise PolicyError(f"image not allowed: {spec.get('Image')!r}")
    if spec.get("User") != SANDBOX_USER:
        raise PolicyError(f"User must be {SANDBOX_USER}")
    if spec.get("NetworkDisabled") is not True:
        raise PolicyError("NetworkDisabled must be true")
    if spec.get("WorkingDir") != "/tmp":
        raise PolicyError("WorkingDir must be /tmp")
    if (spec.get("Labels") or {}).get(SANDBOX_LABEL) != "1":
        raise PolicyError(f"label {SANDBOX_LABEL}=1 is required")

    host = spec.get("HostConfig")
    if not isinstance(host, dict):
        raise PolicyError("HostConfig is required")
    extra = set(host) - HOST_CONFIG_KEYS
    if extra:
        raise PolicyError(f"HostConfig field(s) not allowed: {sorted(extra)}")
    if host.get("NetworkMode") not in ("default", "none"):
        raise PolicyError("NetworkMode must be default or none")
    if host.get("ReadonlyRootfs") is not True:
        raise PolicyError("ReadonlyRootfs must be true")
    if host.get("CapDrop") != ["ALL"]:
        raise PolicyError("CapDrop must be exactly ['ALL']")
    if host.get("SecurityOpt") != ["no-new-privileges"]:
        raise PolicyError("SecurityOpt must be exactly ['no-new-privileges']")
    if set(host.get("Tmpfs") or {}) - {"/tmp"}:
        raise PolicyError("only /tmp may be a tmpfs mount")
    memory = host.get("Memory")
    if not isinstance(memory, int) or not 0 < memory <= MAX_MEMORY:
        raise PolicyError(f"Memory must be between 1 and {MAX_MEMORY} bytes")
    if host.get("MemorySwap") != memory:
        raise PolicyError("MemorySwap must equal Memory (no swap)")
    pids = host.get("PidsLimit")
    if not isinstance(pids, int) or not 0 < pids <= MAX_PIDS:
        raise PolicyError(f"PidsLimit must be between 1 and {MAX_PIDS}")
    cpus = host.get("NanoCpus")
    if not isinstance(cpus, int) or not 0 < cpus <= MAX_NANO_CPUS:
        raise PolicyError("NanoCpus out of range")
    if host.get("Runtime", "runc") not in ALLOWED_RUNTIMES:
        raise PolicyError(f"runtime not allowed: {host.get('Runtime')!r}")


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float | None = None):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not None:
            self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


_known_sandboxes: set[str] = set()
_known_lock = threading.Lock()


def _daemon(method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    conn = UnixConnection(DOCKER_SOCKET)
    conn.request(method, path, body=body, headers=headers or {})
    return conn, conn.getresponse()


def is_sandbox_container(container_id: str) -> bool:
    """True only for containers carrying our label, so the runner can't act on anything else."""
    with _known_lock:
        if container_id in _known_sandboxes:
            return True
    conn, resp = _daemon("GET", f"/containers/{container_id}/json")
    try:
        if resp.status != 200:
            return False
        labels = (json.loads(resp.read()).get("Config") or {}).get("Labels") or {}
    finally:
        conn.close()
    if labels.get(SANDBOX_LABEL) == "1":
        with _known_lock:
            _known_sandboxes.add(container_id)
        return True
    return False


def image_ref(query: dict) -> str:
    name = (query.get("fromImage") or [""])[0]
    tag = (query.get("tag") or [""])[0]
    return name if (":" in name.rsplit("/", 1)[-1] or not tag) else f"{name}:{tag}"


class Handler(BaseHTTPRequestHandler):
    server_version = "judge-docker-policy"
    protocol_version = "HTTP/1.0"  # close-delimited bodies keep streaming responses simple

    def log_message(self, fmt, *args):  # quiet by default; denials are logged explicitly
        pass

    def _deny(self, reason: str) -> None:
        print(f"DENY {self.command} {self.path}: {reason}", flush=True)
        self._reply(403, json.dumps({"message": f"denied by sandbox policy: {reason}"}).encode())

    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self, path: str, body: bytes | None) -> None:
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        try:
            conn, resp = _daemon(self.command, path, body, headers if body is not None else {})
        except OSError as exc:
            self._reply(502, json.dumps({"message": f"docker daemon unreachable: {exc}"}).encode())
            return
        try:
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            self.end_headers()
            while chunk := resp.read(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

    def _handle(self) -> None:
        parts = urlsplit(self.path)
        path = VERSION_PREFIX.sub("", parts.path)
        query = parse_qs(parts.query)
        method = self.command
        target = path + (f"?{parts.query}" if parts.query else "")
        full = parts.path + (f"?{parts.query}" if parts.query else "")  # preserve version prefix upstream
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._deny("request body too large")
        body = self.rfile.read(length) if length else None

        if method == "GET" and path in ("/_ping", "/version"):
            return self._forward(full, None)

        if method == "POST" and path == "/containers/create":
            try:
                check_create(json.loads(body or b"null"))
            except (PolicyError, ValueError) as exc:
                return self._deny(str(exc))
            if set(query) - {"name"}:
                return self._deny("query parameters not allowed on create")
            return self._forward(full, body)

        m = re.fullmatch(rf"/containers/({CONTAINER_ID})(?:/(start|wait|kill|logs|json))?", path)
        if m and (
            (method == "POST" and m.group(2) in ("start", "wait", "kill"))
            or (method == "GET" and m.group(2) in ("logs", "json"))
            or (method == "DELETE" and m.group(2) is None)
        ):
            if not is_sandbox_container(m.group(1)):
                return self._deny("not a sandbox container")
            return self._forward(full, body)

        if method == "POST" and path == "/images/create":
            ref = image_ref(query)
            if ref not in ALLOWED_IMAGES:
                return self._deny(f"image not allowed: {ref!r}")
            return self._forward(full, body)

        m = re.fullmatch(r"/images/(.+)/json", path)
        if method == "GET" and m and m.group(1) in ALLOWED_IMAGES:
            return self._forward(full, None)

        self._deny(f"{method} {target} is not permitted")

    do_GET = do_POST = do_DELETE = do_PUT = do_HEAD = do_PATCH = _handle


def serve(host: str = LISTEN_HOST, port: int = LISTEN_PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    print(f"policy proxy on {LISTEN_HOST}:{LISTEN_PORT} -> {DOCKER_SOCKET}", flush=True)
    print(f"allowed images: {sorted(ALLOWED_IMAGES)}", flush=True)
    serve().serve_forever()
