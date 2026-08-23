"""Port of the frozen stream-limit suite (provider concurrency cap +
shared-stream admission), from scripts/srv1/run_stream_limit_scenarios.py.

Unlike the other five stream-scenario suites, the frozen script here never
wrote a structured per-scenario JSON artifact -- it only printed "Scenario N
PASS/FAIL" and returned one aggregate exit code. This port keeps the two
real, named checks the script actually computes (STREAM-LIMIT-01/02) rather
than collapsing them into a single opaque pass/fail, so a failure in one
doesn't hide a pass in the other.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from agent.suites import suite

from m3undle_lab.api import HoldOpenStream, M3UndleClient
from m3undle_lab.simulator import SimulatorInstance
from m3undle_lab.stream_scenarios import stream_status

SUITE = suite("stream-limit", group="core", order=145)
SIM_PORT = 19101
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-a.json"
PROVIDER_NAME = "provider-stream-limit"
MAX_STREAMS = 2


def _simulator_address() -> tuple[str, str | None]:
    import platform

    from agent.container import get_docker_gateway

    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{SIM_PORT}"
    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{SIM_PORT}"
    return "127.0.0.1", None


class _RecordCollector:
    def __init__(self) -> None:
        self.records: dict[str, tuple[bool | None, str, Any]] = {}

    def record(self, name: str, passed: bool | None, message: str, detail: Any = None) -> None:
        self.records[name] = (passed, message, detail)


def _run_scenario_1(ctx: _RecordCollector, url1: str, url2: str, url3: str) -> None:
    """STREAM-LIMIT-01: Provider cap + shared stream.

    Client A on stream1 -> 200; Client B on stream2 -> 200; Client C on
    stream3 -> 503 (cap exceeded); Client D on stream1 -> 200 (shared).
    """
    print("\n--- Scenario: STREAM-LIMIT-01 Provider Cap + Shared Stream ---")
    c1 = HoldOpenStream(url1, hold_seconds=45)
    c1.start()
    s1 = c1.wait_header()
    print(f"Client1 status={s1}")

    c2 = HoldOpenStream(url2, hold_seconds=45)
    c2.start()
    s2 = c2.wait_header()
    print(f"Client2 status={s2}")

    s3, r3 = stream_status(url3)
    print(f"Client3 status={s3} retry_after={r3!r}")

    s4, _r4 = stream_status(url1)
    print(f"Client4(shared) status={s4}")

    c1.stop()
    c2.stop()

    ok = s1 == 200 and s2 == 200 and s3 == 503 and s4 == 200
    ctx.record(
        "STREAM-LIMIT-01", ok,
        f"client1={s1} client2={s2} client3(over-cap)={s3} client4(shared)={s4}",
        {"client1_status": s1, "client2_status": s2, "client3_status": s3, "client3_retry_after": r3, "client4_status": s4},
    )


def _run_scenario_2(ctx: _RecordCollector, url1: str, url2: str, url3: str) -> None:
    """STREAM-LIMIT-02: Shared-first then cap.

    Client A on stream1 -> 200; Client B on stream1 (same channel) -> 200
    (shared); Client C on stream2 -> 200; Client D on stream3 -> 503 (third
    unique upstream denied).
    """
    print("\n--- Scenario: STREAM-LIMIT-02 Shared-First Then Cap ---")
    a = HoldOpenStream(url1, hold_seconds=45)
    a.start()
    sa = a.wait_header()
    print(f"ClientA status={sa}")

    sb, _rb = stream_status(url1)
    print(f"ClientB(shared same channel) status={sb}")

    c = HoldOpenStream(url2, hold_seconds=45)
    c.start()
    sc = c.wait_header()
    print(f"ClientC(second unique upstream) status={sc}")

    sd, rd = stream_status(url3)
    print(f"ClientD(third unique upstream) status={sd} retry_after={rd!r}")

    a.stop()
    c.stop()

    ok = sa == 200 and sb == 200 and sc == 200 and sd == 503
    ctx.record(
        "STREAM-LIMIT-02", ok,
        f"clientA={sa} clientB(shared)={sb} clientC={sc} clientD(over-cap)={sd}",
        {"client_a_status": sa, "client_b_status": sb, "client_c_status": sc, "client_d_status": sd, "client_d_retry_after": rd},
    )


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    collector = _RecordCollector()
    state: dict[str, object] = {"reason": "Stream-limit suite setup did not complete", "records": collector.records}
    simulator: SimulatorInstance | None = None

    try:
        bind, public_host = _simulator_address()
        simulator = SimulatorInstance(fixture=FIXTURE, port=SIM_PORT, bind=bind, public_host=public_host, suite="stream-limit")
        simulator.start()
        if not simulator.wait_healthy():
            state["reason"] = "Provider simulator did not become healthy"
            return {"state": state}

        client = M3UndleClient(base_url)
        if not client.setup(playlist_url=simulator.playlist_url, provider_name=PROVIDER_NAME, max_concurrent_streams=MAX_STREAMS):
            state["reason"] = client.last_setup_error or "Setup sequence failed"
            return {"state": state}

        # A short grace period after each reset -- the original script's own timing --
        # gives M3Undle time to actually clear cooldown/session state before the next
        # scenario starts; without it STREAM-LIMIT-02 can see stale state from
        # STREAM-LIMIT-01 (confirmed for real: ClientA got a stale 503).
        client.reset_debug_state()
        time.sleep(1)
        urls = client.get_stream_urls()
        if len(urls) < 3:
            state["reason"] = f"need >= 3 stream urls, found {len(urls)}"
            return {"state": state}

        _run_scenario_1(collector, urls[0], urls[1], urls[2])
        client.reset_debug_state()
        time.sleep(1)
        _run_scenario_2(collector, urls[0], urls[1], urls[2])

        if client.provider_id:
            client.delete_provider_with_retry(client.provider_id)

        state["reason"] = None
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"Stream-limit suite setup failed: {exc}"
        return {"state": state}
    finally:
        if simulator is not None:
            try:
                simulator.stop()
            except Exception:
                pass


def _register_case(case_id: str) -> None:
    @SUITE.case(case_id)
    def _replay(ctx, state):
        records: dict[str, tuple[bool | None, str, Any]] = state["records"]  # type: ignore[assignment]
        entry = records.get(case_id)
        if entry is None:
            ctx.skip(case_id, str(state.get("reason") or "Stream-limit suite setup did not reach this case"))
            return
        passed, message, detail = entry
        ctx.record(case_id, passed, message, detail)


for _case_id in ("STREAM-LIMIT-01", "STREAM-LIMIT-02"):
    _register_case(_case_id)
