"""Port of the frozen endpoint-security suite, registered case-by-case with se-lab."""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

from agent.container import get_docker_gateway
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("security", group="core", order=110)
SIM_PORT = 19010
SIM_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-a.json"
TEST_USER = "testuser"
TEST_PASSWORD = "testpass"


def _simulator_address() -> tuple[str, str | None]:
    """Return the listener and advertised URL suitable for this Docker host."""
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{SIM_PORT}"

    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{SIM_PORT}"
    return "127.0.0.1", None


def _disable_auth(client: M3UndleClient) -> bool:
    status, _ = client._request("PUT", "/api/v1/settings/endpoint-security", body={"enabled": False})
    return status in (200, 204)


def _enable_auth(client: M3UndleClient) -> bool:
    status, _ = client._request(
        "PUT",
        "/api/v1/settings/endpoint-security",
        body={"enabled": True, "username": TEST_USER, "password": TEST_PASSWORD},
    )
    return status in (200, 204)


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    state: dict[str, object] = {
        "ready": False,
        "reason": "Security suite setup did not complete",
        "client": None,
        "simulator": None,
    }
    try:
        bind, public_host = _simulator_address()
        simulator = SimulatorInstance(
            fixture=SIM_FIXTURE,
            port=SIM_PORT,
            bind=bind,
            public_host=public_host,
            suite="security",
        )
        state["simulator"] = simulator
        simulator.start()
        if not simulator.wait_healthy():
            state["reason"] = "Provider simulator did not become healthy"
            return {"state": state}

        client = M3UndleClient(base_url)
        state["client"] = client
        if not client.setup(playlist_url=simulator.playlist_url, provider_name="provider-sec-sim"):
            state["reason"] = client.last_setup_error or "M3Undle provider setup failed"
            return {"state": state}
        if not _disable_auth(client):
            state["reason"] = "Could not disable endpoint auth before security cases"
            return {"state": state}

        state["ready"] = True
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"Security suite setup failed: {exc}"
        return {"state": state}


def _ready(ctx: Any, state: dict[str, object], test_id: str) -> M3UndleClient | None:
    client = state.get("client")
    if state.get("ready") and isinstance(client, M3UndleClient):
        return client
    ctx.skip(test_id, str(state.get("reason") or "Security suite setup did not complete"))
    return None


@SUITE.case("SEC-01")
def sec_01(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "SEC-01")
    if client is None:
        return
    if not _enable_auth(client):
        ctx.fail("SEC-01", "Could not enable endpoint auth via PUT /api/v1/settings/endpoint-security")
        return
    status, _ = client.get("/m3u/m3undle.m3u")
    ctx.record("SEC-01", status in (401, 403), f"expected 401 or 403, got {status}")


@SUITE.case("SEC-02")
def sec_02(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "SEC-02")
    if client is None:
        return
    status, body = client.get(f"/m3u/m3undle.m3u?username={TEST_USER}&password={TEST_PASSWORD}")
    is_m3u = isinstance(body, str) and "#EXTM3U" in body
    ctx.record("SEC-02", status == 200 and is_m3u, f"status={status} is_m3u={is_m3u}")


@SUITE.case("SEC-03")
def sec_03(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "SEC-03")
    if client is None:
        return
    status, _ = client.get(f"/m3u/m3undle.m3u?username={TEST_USER}&password=wrongpass")
    ctx.record("SEC-03", status == 401, f"expected 401, got {status}")


@SUITE.case("SEC-04")
def sec_04(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "SEC-04")
    if client is None:
        return
    if not _disable_auth(client):
        ctx.fail("SEC-04", "Could not disable endpoint auth")
        return
    status, body = client.get("/m3u/m3undle.m3u")
    is_m3u = isinstance(body, str) and "#EXTM3U" in body
    ctx.record("SEC-04", status == 200 and is_m3u, f"status={status} is_m3u={is_m3u}")


@SUITE.case("SEC-05")
def sec_05(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "SEC-05")
    if client is None:
        return
    if not _enable_auth(client):
        ctx.fail("SEC-05", "Could not enable endpoint auth")
        return
    status, _ = client.get("/live/wronguser/wrongpass/99999")
    ctx.record("SEC-05", status in (401, 403), f"expected 401 or 403, got {status}")
    _disable_auth(client)


@SUITE.case("SEC-06")
def sec_06(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "SEC-06")
    if client is None:
        return
    if not _enable_auth(client):
        ctx.fail("SEC-06", "Could not enable endpoint auth")
        return
    status, _ = client.get("/health/ready")
    ctx.record("SEC-06", status in (200, 503), f"/health/ready returned {status} (expected 200 or 503)")
    _disable_auth(client)


@SUITE.teardown
def teardown(state: dict[str, object]) -> None:
    client = state.get("client")
    if isinstance(client, M3UndleClient):
        try:
            _disable_auth(client)
        except Exception:
            pass

    simulator = state.get("simulator")
    if isinstance(simulator, SimulatorInstance):
        try:
            simulator.stop()
        except Exception:
            pass
