"""Port of the frozen status and monitoring suite, registered case-by-case with se-lab."""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

from agent.container import get_docker_gateway
from agent.suites import suite

from m3undle_lab.api import HoldOpenStream, M3UndleClient
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("monitoring", group="core", order=120)
SIM_PORT = 19014
SIM_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-a.json"


def _simulator_address() -> tuple[str, str | None]:
    """Return the listener and advertised URL suitable for this Docker host."""
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{SIM_PORT}"

    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{SIM_PORT}"
    return "127.0.0.1", None


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    state: dict[str, object] = {
        "ready": False,
        "reason": "Monitoring suite setup did not complete",
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
            suite="monitoring",
        )
        state["simulator"] = simulator
        simulator.start()
        if not simulator.wait_healthy():
            state["reason"] = "Provider simulator did not become healthy"
            return {"state": state}

        client = M3UndleClient(base_url)
        state["client"] = client
        if not client.setup(playlist_url=simulator.playlist_url, provider_name="provider-mon-sim"):
            state["reason"] = client.last_setup_error or "M3Undle provider setup failed"
            return {"state": state}

        state["ready"] = True
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"Monitoring suite setup failed: {exc}"
        return {"state": state}


def _ready(ctx: Any, state: dict[str, object], test_id: str) -> M3UndleClient | None:
    client = state.get("client")
    if state.get("ready") and isinstance(client, M3UndleClient):
        return client
    ctx.skip(test_id, str(state.get("reason") or "Monitoring suite setup did not complete"))
    return None


@SUITE.case("MON-01")
def mon_01(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "MON-01")
    if client is None:
        return
    status, body = client.get("/status")
    if status != 200 or not isinstance(body, dict):
        ctx.fail("MON-01", f"GET /status returned {status}: {str(body)[:80]}")
        return
    lineup = body.get("lineup") if isinstance(body.get("lineup"), dict) else {}
    has_lineup = "lineupStatus" in body or "lineup_status" in body or "status" in body or bool(lineup)
    has_profile = "activeProfile" in body or "active_profile" in body or "activeProfile" in lineup
    has_snapshot = "activeSnapshot" in body or "active_snapshot" in body or "snapshotId" in body or "activeSnapshot" in lineup
    ctx.record(
        "MON-01",
        has_lineup or has_profile or has_snapshot,
        f"lineupStatus={has_lineup} activeProfile={has_profile} activeSnapshot={has_snapshot} keys={list(body.keys())[:10]}",
    )


@SUITE.case("MON-02")
def mon_02(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "MON-02")
    if client is None:
        return
    status, body = client.get("/status/streams")
    if status == 404:
        ctx.fail("MON-02", "endpoint /status/streams not found (404)")
        return
    is_list = isinstance(body, list)
    is_summary = isinstance(body, dict) and "activeSessionCount" in body
    ctx.record("MON-02", status == 200 and (is_list or is_summary), f"response is list={is_list} summary={is_summary}")


@SUITE.case("MON-03")
def mon_03(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "MON-03")
    if client is None:
        return
    status, body = client.get("/status/streams/clients")
    if status == 404:
        ctx.fail("MON-03", "endpoint /status/streams/clients not found (404)")
        return
    ctx.record(
        "MON-03",
        status in (200, 204) and (body is None or isinstance(body, list) or isinstance(body, dict)),
        f"status={status} type={type(body).__name__}",
    )


@SUITE.case("MON-04")
def mon_04(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "MON-04")
    if client is None:
        return
    status, body = client.get("/status/streams/providers")
    if status == 404:
        ctx.fail("MON-04", "endpoint /status/streams/providers not found (404)")
        return
    ctx.record("MON-04", status in (200, 204), f"status={status} type={type(body).__name__}")


@SUITE.case("MON-05")
def mon_05(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "MON-05")
    if client is None:
        return
    stream_urls = client.get_stream_urls()
    if not stream_urls:
        ctx.fail("MON-05", "No stream URLs available for monitoring session test")
        return

    holder = HoldOpenStream(stream_urls[0], hold_seconds=15.0)
    try:
        holder.start()
        header_status = holder.wait_header()
        if header_status != 200:
            ctx.fail("MON-05", f"stream open returned {header_status} error={holder.error!r}")
            return

        sessions = client.wait_for_active_session_count(1, timeout_seconds=10.0)
        if not sessions:
            ctx.fail("MON-05", "No active session appeared in /status/streams")
            return

        session_id = str(sessions[0].get("sessionId") or sessions[0].get("SessionId") or "")
        session = client.get_session_status(session_id) if session_id else None
        valid = bool(
            session
            and str(session.get("sessionId") or session.get("SessionId") or "") == session_id
            and (session.get("subscriberCount") or session.get("SubscriberCount") or 0) >= 1
        )
        ctx.record(
            "MON-05",
            valid,
            " ".join(
                [
                    f"session_id={session_id or 'n/a'}",
                    f"stream_status={header_status}",
                    f"bytes_read={holder.bytes_read}",
                    f"session_found={bool(session)}",
                ]
            ),
        )
    finally:
        holder.stop()


@SUITE.case("MON-06")
def mon_06(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "MON-06")
    if client is None:
        return
    status, body = client.get("/health/ready")
    ctx.record("MON-06", status == 200, f"/health/ready after setup returned {status} body={str(body)[:80]}")


@SUITE.case("DASH-01")
def dash_01(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "DASH-01")
    if client is None:
        return
    status, body = client.get("/api/v1/dashboard/stats")
    if status == 404:
        ctx.fail("DASH-01", "endpoint /api/v1/dashboard/stats not found (404)")
        return
    if status != 200 or not isinstance(body, dict):
        ctx.fail("DASH-01", f"status={status} type={type(body).__name__}")
        return
    has_numbers = any(isinstance(value, (int, float)) for value in body.values())
    ctx.record("DASH-01", has_numbers, f"keys={list(body.keys())[:10]}")


@SUITE.teardown
def teardown(state: dict[str, object]) -> None:
    simulator = state.get("simulator")
    if isinstance(simulator, SimulatorInstance):
        try:
            simulator.stop()
        except Exception:
            pass
