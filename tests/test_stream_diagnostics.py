"""Port of the frozen stream-diagnostics suite, from
scripts/srv1/run_stream_diagnostics_scenarios.py.

Validates issue #66 observability using the provider simulator and
M3Undle's /status/streams/events and /debug/streams/rca endpoints.

Structural adaptation, not a behavior change: same collector-replay pattern
already used for test_profiles.py/test_xtream.py/test_hdhr.py/
test_stream_cooldown.py.
"""

from __future__ import annotations

import platform
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.container import get_docker_gateway
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.simulator import SimulatorInstance
from m3undle_lab.stream_scenarios import get_json, iter_chunks_with_deadline, post_json

SUITE = suite("stream-diagnostics", group="core", order=147)
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "providers"

EVENT = {"SubscriberRemoved": 2, "FirstUpstreamByte": 5, "UpstreamFailure": 6, "ReconnectScheduled": 7, "CooldownRecorded": 9}


@dataclass(frozen=True)
class ScenarioProvider:
    fixture: str
    provider_name: str
    port: int
    max_streams: int = 4


def _simulator_address(port: int) -> tuple[str, str | None]:
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{port}"
    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{port}"
    return "127.0.0.1", None


class _RecordCollector:
    def __init__(self) -> None:
        self.records: dict[str, tuple[bool | None, str, Any]] = {}

    def record(self, name: str, passed: bool | None, message: str, detail: Any = None) -> None:
        self.records[name] = (passed, message, detail)


def add_reason(reasons: list[str], condition: bool, message: str) -> None:
    if not condition:
        reasons.append(message)


def provider_state(port: int) -> dict[str, Any]:
    status, body = get_json(f"http://127.0.0.1:{port}/debug/state", timeout=10)
    if status == 200 and isinstance(body, dict):
        return body
    return {"status_code": status, "response": body}


def get_events(m3undle_url: str) -> list[dict[str, Any]]:
    status, body = get_json(f"{m3undle_url}/status/streams/events", timeout=10)
    if status != 200 or not isinstance(body, list):
        raise RuntimeError(f"events endpoint failed: {status} {body}")
    return [item for item in body if isinstance(item, dict)]


def get_rca(m3undle_url: str) -> dict[str, Any]:
    status, body = get_json(f"{m3undle_url}/debug/streams/rca", timeout=10)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"rca endpoint failed: {status} {body}")
    return body


def event_kind(event: dict[str, Any]) -> int | str | None:
    return event.get("kind") if "kind" in event else event.get("Kind")


def event_number(value: int | str | None) -> int | str | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return EVENT.get(value, value)
    return value


def event_kinds(events: list[dict[str, Any]]) -> list[int | str | None]:
    return [event_number(event_kind(event)) for event in events]


def has_event(events: list[dict[str, Any]], kind: int) -> bool:
    return any(event_number(event_kind(event)) == kind for event in events)


def wait_for_event(m3undle_url: str, kind: int, timeout_seconds: float = 20.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    latest: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        latest = get_events(m3undle_url)
        if any(event_number(event_kind(event)) == kind for event in latest):
            return latest
        time.sleep(0.5)
    return latest


def read_and_close(url: str, read_bytes: int = 1024) -> int:
    import urllib.request

    with urllib.request.urlopen(urllib.request.Request(url), timeout=20.0) as resp:
        status = resp.status
        if status == 200:
            resp.read(read_bytes)
        return status


def read_until_timeout_or_close(url: str, timeout_seconds: float = 40.0) -> int | None:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout_seconds) as resp:
            for _chunk in iter_chunks_with_deadline(resp, chunk_size=1024, deadline_seconds=timeout_seconds):
                pass
            return resp.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code
    except Exception:
        return None


def setup_one_provider(m3undle_url: str, provider: ScenarioProvider) -> tuple[SimulatorInstance, str, list[str]]:
    bind, public_host = _simulator_address(provider.port)
    proc = SimulatorInstance(fixture=provider.fixture, port=provider.port, bind=bind, public_host=public_host, suite=f"diagnostics-{provider.provider_name}")
    try:
        proc.start()
        if not proc.wait_healthy():
            raise RuntimeError("provider_sim did not start in time")

        client = M3UndleClient(m3undle_url)
        if not client.setup(playlist_url=proc.playlist_url, provider_name=provider.provider_name, max_concurrent_streams=provider.max_streams):
            raise RuntimeError(client.last_setup_error or "provider setup failed")
        client.reset_debug_state()
        urls = client.get_stream_urls()
        return proc, client.provider_id or "", urls
    except Exception:
        proc.stop()
        raise


def teardown(m3undle_url: str, provider_id: str, proc: SimulatorInstance | None) -> None:
    post_json(f"{m3undle_url}/debug/streams/reset")
    post_json(f"{m3undle_url}/debug/strikes/reset")
    if provider_id:
        M3UndleClient(m3undle_url).delete_provider_with_retry(provider_id)
    if proc is not None:
        proc.stop()


def scenario_client_disconnect(ctx: _RecordCollector, m3undle_url: str) -> None:
    print("\n--- Scenario: client disconnect diagnostics ---")
    provider = ScenarioProvider(str(FIXTURES_DIR / "provider-a.json"), "provider-diagnostics-disconnect", 19661)
    proc = None
    provider_id = ""
    try:
        proc, provider_id, urls = setup_one_provider(m3undle_url, provider)
        status = read_and_close(urls[0])
        events = wait_for_event(m3undle_url, EVENT["SubscriberRemoved"], timeout_seconds=15)
        rca = get_rca(m3undle_url)
        recent_events = rca.get("recentEvents") or rca.get("RecentEvents")

        reasons: list[str] = []
        add_reason(reasons, status == 200, "stream request did not return 200")
        add_reason(reasons, has_event(events, EVENT["FirstUpstreamByte"]), "diagnostics missed FirstUpstreamByte")
        add_reason(reasons, has_event(events, EVENT["SubscriberRemoved"]), "diagnostics missed SubscriberRemoved")
        add_reason(reasons, isinstance(recent_events, list), "RCA recentEvents was not a list")
        print("PASS" if not reasons else "FAIL")
        for reason in reasons:
            print(f"  - {reason}")
        ctx.record(
            "DIAG-DISCONNECT", not reasons, "; ".join(reasons) if reasons else "all disconnect-diagnostics assertions passed",
            {"status": status, "diagnostic_event_kinds": event_kinds(events), "provider_counts": provider_state(provider.port)},
        )
    except Exception as exc:
        ctx.record("DIAG-DISCONNECT", False, str(exc))
    finally:
        teardown(m3undle_url, provider_id, proc)


def scenario_stall_reconnect(ctx: _RecordCollector, m3undle_url: str) -> None:
    print("\n--- Scenario: stall/reconnect diagnostics ---")
    provider = ScenarioProvider(str(FIXTURES_DIR / "provider-diagnostics-stall.json"), "provider-diagnostics-stall", 19662)
    proc = None
    provider_id = ""
    try:
        proc, provider_id, urls = setup_one_provider(m3undle_url, provider)
        # Keep the request open long enough for M3Undle's read-stall timeout to fire.
        status = read_until_timeout_or_close(urls[0], timeout_seconds=40)
        events = wait_for_event(m3undle_url, EVENT["ReconnectScheduled"], timeout_seconds=35)

        reasons: list[str] = []
        add_reason(reasons, has_event(events, EVENT["UpstreamFailure"]), "diagnostics missed UpstreamFailure")
        add_reason(reasons, has_event(events, EVENT["ReconnectScheduled"]), "diagnostics missed ReconnectScheduled")
        print("PASS" if not reasons else "FAIL")
        for reason in reasons:
            print(f"  - {reason}")
        ctx.record(
            "DIAG-STALL-RECONNECT", not reasons, "; ".join(reasons) if reasons else "all stall/reconnect-diagnostics assertions passed",
            {"status": status, "diagnostic_event_kinds": event_kinds(events), "provider_counts": provider_state(provider.port)},
        )
    except Exception as exc:
        ctx.record("DIAG-STALL-RECONNECT", False, str(exc))
    finally:
        teardown(m3undle_url, provider_id, proc)


def scenario_cooldown_rca(ctx: _RecordCollector, m3undle_url: str) -> None:
    print("\n--- Scenario: cooldown RCA bundle ---")
    provider = ScenarioProvider(str(FIXTURES_DIR / "provider-cooldown-429.json"), "provider-diagnostics-cooldown", 19663)
    proc = None
    provider_id = ""
    try:
        proc, provider_id, urls = setup_one_provider(m3undle_url, provider)
        status = read_and_close(urls[0])
        events = wait_for_event(m3undle_url, EVENT["CooldownRecorded"], timeout_seconds=15)
        rca = get_rca(m3undle_url)
        cooldowns = rca.get("activeCooldowns") or rca.get("ActiveCooldowns") or []
        recent_events = rca.get("recentEvents") or rca.get("RecentEvents") or []

        reasons: list[str] = []
        add_reason(reasons, status == 503, "cooldown request did not return 503")
        add_reason(reasons, has_event(events, EVENT["CooldownRecorded"]), "diagnostics missed CooldownRecorded")
        add_reason(reasons, isinstance(cooldowns, list) and len(cooldowns) > 0, "RCA missed active cooldowns")
        add_reason(reasons, isinstance(recent_events, list) and len(recent_events) > 0, "RCA missed recent events")
        print("PASS" if not reasons else "FAIL")
        for reason in reasons:
            print(f"  - {reason}")
        ctx.record(
            "DIAG-COOLDOWN-RCA", not reasons, "; ".join(reasons) if reasons else "all cooldown-RCA assertions passed",
            {"status": status, "diagnostic_event_kinds": event_kinds(events), "active_cooldown_count": len(cooldowns), "provider_counts": provider_state(provider.port)},
        )
    except Exception as exc:
        ctx.record("DIAG-COOLDOWN-RCA", False, str(exc))
    finally:
        teardown(m3undle_url, provider_id, proc)


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    collector = _RecordCollector()
    state: dict[str, object] = {"reason": "Stream-diagnostics suite setup did not complete", "records": collector.records}
    try:
        scenario_client_disconnect(collector, base_url)
        scenario_stall_reconnect(collector, base_url)
        scenario_cooldown_rca(collector, base_url)
        state["reason"] = None
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"Stream-diagnostics suite setup failed: {exc}"
        return {"state": state}


def _register_case(case_id: str) -> None:
    @SUITE.case(case_id)
    def _replay(ctx, state):
        records: dict[str, tuple[bool | None, str, Any]] = state["records"]  # type: ignore[assignment]
        entry = records.get(case_id)
        if entry is None:
            ctx.skip(case_id, str(state.get("reason") or "Stream-diagnostics suite setup did not reach this case"))
            return
        passed, message, detail = entry
        ctx.record(case_id, passed, message, detail)


for _case_id in ("DIAG-DISCONNECT", "DIAG-STALL-RECONNECT", "DIAG-COOLDOWN-RCA"):
    _register_case(_case_id)
