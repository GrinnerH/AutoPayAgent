import os
import threading
import time
from typing import List, Optional

import httpx
import socket
import uvicorn

from src.facilitator_server import app as facilitator_app
from src.mock_server import ServiceConfig, start_server

_STARTED = False


def _start_facilitator() -> Optional[threading.Thread]:
    facilitator_host = os.getenv("FACILITATOR_HOST", "127.0.0.1")
    facilitator_port = int(os.getenv("FACILITATOR_PORT", "9000"))
    base_url = f"http://{facilitator_host}:{facilitator_port}"
    def _port_in_use(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex((host, port)) == 0
    try:
        resp = httpx.get(f"{base_url}/openapi.json", timeout=1.0)
        if resp.status_code == 200:
            os.environ.setdefault("FACILITATOR_URL", base_url)
            return None
    except httpx.HTTPError:
        pass
    if _port_in_use(facilitator_host, facilitator_port):
        os.environ.setdefault("FACILITATOR_URL", base_url)
        return None

    thread = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": facilitator_app, "host": facilitator_host, "port": facilitator_port, "log_level": "warning"},
        daemon=True,
    )
    thread.start()
    os.environ.setdefault("FACILITATOR_URL", base_url)
    for _ in range(10):
        try:
            if httpx.get(f"{base_url}/openapi.json", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    return thread


def _start_demo_services() -> List[object]:
    servers = []
    def _port_in_use(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex((host, port)) == 0

    def _maybe_start(config: ServiceConfig) -> None:
        if _port_in_use(config.host, config.port):
            return
        try:
            servers.append(start_server(config))
        except OSError:
            return
    _maybe_start(
        ServiceConfig(
            name="Mock Free Info",
            host="127.0.0.1",
            port=18079,
            path="/api/free",
            amount_usdc=0.0,
            payee="0x0000000000000000000000000000000000000000",
            free=True,
        )
    )
    _maybe_start(
        ServiceConfig(
            name="Mock Weather Free",
            host="127.0.0.1",
            port=18080,
            path="/api/weather-free",
            amount_usdc=0.0,
            payee="0x0000000000000000000000000000000000000000",
            free=True,
        )
    )
    _maybe_start(
        ServiceConfig(
            name="Mock Weather Pro",
            host="127.0.0.1",
            port=18081,
            path="/api/weather",
            amount_usdc=0.05,
            payee="0x1111111111111111111111111111111111111111",
        )
    )
    _maybe_start(
        ServiceConfig(
            name="Mock Market Intel",
            host="127.0.0.1",
            port=18082,
            path="/api/reports/coinbase",
            amount_usdc=5.00,
            payee="0x2222222222222222222222222222222222222222",
        )
    )
    time.sleep(0.2)
    return servers


def ensure_services_started() -> None:
    global _STARTED
    if _STARTED:
        return
    _start_facilitator()
    _start_demo_services()
    _STARTED = True
