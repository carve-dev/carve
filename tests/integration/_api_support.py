"""Helpers for the REST-API integration tests (not collected — leading underscore).

Builds a real :class:`StateStore` over a per-test Postgres, mints a token, and
(for the lifecycle test) runs the app under uvicorn on a free port.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import uvicorn

from carve.core.config.schema import (
    ApiConfig,
    Config,
    CorsConfig,
    ModelsConfig,
    ProjectConfig,
    ServerConfig,
)
from carve.core.config.state_store import StateStoreConfig
from carve.core.state.database import (
    create_engine_from_config,
    create_session_factory,
    initialize_database,
)
from carve.core.state.store import StateStore


def make_config(url: str, *, port: int = 8765) -> Config:
    return Config(
        project=ProjectConfig(name="api-integration"),
        models=ModelsConfig(anthropic_api_key="sk-test"),
        server=ServerConfig(),
        api=ApiConfig(host="127.0.0.1", port=port, cors=CorsConfig()),
        state_store=StateStoreConfig(url=url),
    )


def make_state_store(url: str) -> StateStore:
    engine = create_engine_from_config(make_config(url))
    initialize_database(engine)
    return StateStore(create_session_factory(engine))


def mint_token(store: StateStore) -> str:
    _token_id, plaintext = store.tokens.create(scopes=["*"])
    return plaintext


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def running_server(app: object, port: int) -> Iterator[str]:
    """Run ``app`` under uvicorn on ``127.0.0.1:port`` in a thread; yield base URL."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_until_up(base)
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _wait_until_up(base: str, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.05)
    raise RuntimeError("server did not come up in time")


def project_paths(tmp: Path) -> object:
    from carve.core.config.paths import ProjectPaths

    return ProjectPaths.from_root(tmp)


@contextlib.contextmanager
def subscriber_server(received: list[dict], *, status_code: int) -> Iterator[str]:
    """A loopback HTTP subscriber that records POSTs and returns ``status_code``.

    Each captured request is appended to ``received`` as
    ``{"headers": {lower: value}, "body": bytes}``.
    """

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", 0))
            body = self.rfile.read(length)
            received.append(
                {"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
            )
            self.send_response(status_code)
            self.end_headers()

        def log_message(self, *args: object) -> None:  # silence the server log
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _host, port = server.server_address
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
