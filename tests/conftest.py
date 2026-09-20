"""Shared fixtures. The target app runs in-process so tests need no setup step.

If an instance is already listening on a tenant's port (a developer running
`python -m target_app.serve` in another terminal), that instance is reused
rather than fighting it for the port.
"""

from __future__ import annotations

import socket
import threading

import pytest
from werkzeug.serving import make_server

from src.surface.web_playwright import WebSurface
from target_app.app import create_app
from target_app.tenants import TENANTS


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def target_servers():
    servers = []
    for tenant_id, cfg in TENANTS.items():
        if _port_in_use(cfg.port):
            continue
        srv = make_server("127.0.0.1", cfg.port, create_app(tenant_id), threaded=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    yield {tid: f"http://localhost:{cfg.port}" for tid, cfg in TENANTS.items()}
    for srv in servers:
        srv.shutdown()


@pytest.fixture
def surface(target_servers):
    s = WebSurface(headless=True, timeout_ms=4000)
    try:
        yield s
    finally:
        s.close()
