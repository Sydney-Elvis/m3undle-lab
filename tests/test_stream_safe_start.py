"""Port of the frozen MPEG-TS safe-start suite, from
scripts/srv1/run_stream_safe_start_scenarios.py.

Validates issue #64 -- TS-aligned delivery for late-joining subscribers,
including the packet-boundary fallback when a reconnect has no IDR frame to
safe-start from.

Structural adaptation, not a behavior change: same collector-replay pattern
already used for test_profiles.py/test_xtream.py/test_hdhr.py/
test_stream_cooldown.py/test_stream_diagnostics.py/test_stream_health.py.
"""

from __future__ import annotations

import platform
import time
from pathlib import Path
from typing import Any

from agent.container import get_docker_gateway
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.simulator import SimulatorInstance
from m3undle_lab.stream_scenarios import get_json, post_json, read_stream_bytes

SUITE = suite("stream-safe-start", group="core", order=149)
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios" / "core"

EVENT_RECONNECT_SCHEDULED = 7
EVENT_MPEGTS_SAFE_START_SELECTED = 14
TS_PACKET_SIZE = 188

CASE_IDS = ["TS-SAFE-01", "TS-SAFE-02", "TS-SAFE-03"]


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


def get_events(m3undle_url: str) -> list[dict[str, Any]]:
    status, body = get_json(f"{m3undle_url}/status/streams/events", timeout=10)
    if status != 200 or not isinstance(body, list):
        raise RuntimeError(f"events endpoint failed: {status} {body}")
    return [item for item in body if isinstance(item, dict)]


def _event_kind_int(event: dict[str, Any]) -> int | None:
    raw = event.get("kind") if "kind" in event else event.get("Kind")
    return raw if isinstance(raw, int) else None


def _safe_start_kind(event: dict[str, Any]) -> str | None:
    raw = event.get("safeStartKind") if "safeStartKind" in event else event.get("SafeStartKind")
    return raw if isinstance(raw, str) else None


def count_events(events: list[dict[str, Any]], kind: int) -> int:
    return sum(1 for e in events if _event_kind_int(e) == kind)


def wait_for_event_count(m3undle_url: str, kind: int, minimum: int = 1, timeout_seconds: float = 30.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    latest: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        latest = get_events(m3undle_url)
        if count_events(latest, kind) >= minimum:
            return latest
        time.sleep(0.5)
    return latest


def is_ts_aligned(data: bytes) -> bool:
    if len(data) < TS_PACKET_SIZE:
        return False
    for offset in range(0, len(data) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
        if data[offset] != 0x47:
            return False
    return True


def event_kinds(events: list[dict[str, Any]]) -> list[int | None]:
    return [_event_kind_int(event) for event in events]


def provider_state(port: int) -> dict[str, Any]:
    status, body = get_json(f"http://127.0.0.1:{port}/debug/state", timeout=10)
    if status == 200 and isinstance(body, dict):
        return body
    return {"status_code": status, "response": body}


def setup_one_provider(m3undle_url: str, fixture: str, provider_name: str, port: int) -> tuple[SimulatorInstance, str, list[str]]:
    bind, public_host = _simulator_address(port)
    proc = SimulatorInstance(fixture=fixture, port=port, bind=bind, public_host=public_host, suite=f"safe-start-{provider_name}")
    try:
        proc.start()
        if not proc.wait_healthy():
            raise RuntimeError("provider_sim did not start in time")

        client = M3UndleClient(m3undle_url)
        if not client.setup(playlist_url=proc.playlist_url, provider_name=provider_name, max_concurrent_streams=4):
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


def scenario_ts_safe_start_basic(ctx: _RecordCollector, m3undle_url: str) -> None:
    """TS-SAFE-01: Connect a subscriber, wait for MpegTsSafeStartSelected, verify 0x47 alignment."""
    print("\n--- Scenario: TS-SAFE-01 basic safe-start detection ---")
    fixture = str(FIXTURES_DIR / "provider-ts-safestart.json")
    provider_port = 19671
    proc = None
    provider_id = ""
    try:
        proc, provider_id, urls = setup_one_provider(m3undle_url, fixture, "provider-ts-safestart-basic", provider_port)
        stream_url = next((u for u in urls if "ts-channel-continuous" in u), urls[0])

        initial_bytes = read_stream_bytes(stream_url, 940, timeout=40.0)
        events = wait_for_event_count(m3undle_url, EVENT_MPEGTS_SAFE_START_SELECTED, minimum=1, timeout_seconds=20)

        safe_start_count = count_events(events, EVENT_MPEGTS_SAFE_START_SELECTED)
        data = read_stream_bytes(stream_url, TS_PACKET_SIZE * 3) if safe_start_count else b""
        aligned = is_ts_aligned(data)
        reasons: list[str] = []
        add_reason(reasons, safe_start_count >= 1, "MpegTsSafeStartSelected was not recorded")
        add_reason(reasons, aligned, "late subscriber sample was not TS aligned")
        print("PASS" if not reasons else "FAIL")
        for reason in reasons:
            print(f"  - {reason}")
        ctx.record(
            "TS-SAFE-01", not reasons, "; ".join(reasons) if reasons else "safe-start detected and late subscriber sample TS aligned",
            {"initial_bytes_read": len(initial_bytes), "safe_start_event_count": safe_start_count, "diagnostic_event_kinds": event_kinds(events), "late_subscriber_ts_aligned": aligned, "provider_counts": provider_state(provider_port)},
        )
    except Exception as exc:
        ctx.record("TS-SAFE-01", False, str(exc))
    finally:
        teardown(m3undle_url, provider_id, proc)


def scenario_ts_safe_start_reconnect(ctx: _RecordCollector, m3undle_url: str) -> None:
    """TS-SAFE-02: After stall + reconnect, late subscriber receives TS-aligned data."""
    print("\n--- Scenario: TS-SAFE-02 reconnect preserves safe-start ---")
    fixture = str(FIXTURES_DIR / "provider-ts-safestart.json")
    provider_port = 19672
    proc = None
    provider_id = ""
    try:
        proc, provider_id, urls = setup_one_provider(m3undle_url, fixture, "provider-ts-safestart-reconnect", provider_port)
        stall_url = next((u for u in urls if "ts-channel-stall" in u), urls[0])

        # Read until the first safe-start selection appears, mirroring the original's
        # inline-polling read loop rather than a fixed byte count.
        initial_bytes_read = _read_until_safe_start(stall_url, m3undle_url, timeout_seconds=60.0)

        events = wait_for_event_count(m3undle_url, EVENT_MPEGTS_SAFE_START_SELECTED, minimum=2, timeout_seconds=45)
        safe_start_count = count_events(events, EVENT_MPEGTS_SAFE_START_SELECTED)
        reconnect_count = count_events(events, EVENT_RECONNECT_SCHEDULED)
        data = read_stream_bytes(stall_url, TS_PACKET_SIZE * 3) if safe_start_count >= 2 else b""
        aligned = is_ts_aligned(data)
        reasons: list[str] = []
        add_reason(reasons, safe_start_count >= 2, "reconnect did not record a second safe-start selection")
        add_reason(reasons, reconnect_count >= 1, "diagnostics missed reconnect scheduling")
        add_reason(reasons, aligned, "late subscriber sample was not TS aligned after reconnect")
        print("PASS" if not reasons else "FAIL")
        for reason in reasons:
            print(f"  - {reason}")
        ctx.record(
            "TS-SAFE-02", not reasons, "; ".join(reasons) if reasons else "reconnect recorded a second safe-start selection, sample TS aligned",
            {"initial_bytes_read": initial_bytes_read, "safe_start_event_count": safe_start_count, "reconnect_event_count": reconnect_count, "diagnostic_event_kinds": event_kinds(events), "late_subscriber_ts_aligned": aligned, "provider_counts": provider_state(provider_port)},
        )
    except Exception as exc:
        ctx.record("TS-SAFE-02", False, str(exc))
    finally:
        teardown(m3undle_url, provider_id, proc)


def scenario_ts_safe_start_reconnect_no_idr(ctx: _RecordCollector, m3undle_url: str) -> None:
    """TS-SAFE-03: After stall + reconnect onto a stream with no IDR, M3Undle falls
    back to a byte-count-based (FallbackPacketBoundary) safe start."""
    print("\n--- Scenario: TS-SAFE-03 reconnect without an IDR frame falls back ---")
    fixture = str(SCENARIOS_DIR / "ts-safe-start-reconnect-no-idr.yaml")
    provider_port = 19673
    proc = None
    provider_id = ""
    try:
        proc, provider_id, urls = setup_one_provider(m3undle_url, fixture, "provider-ts-safestart-no-idr", provider_port)
        stall_url = next((u for u in urls if "ts-channel-stall-no-idr" in u), urls[0])

        initial_bytes_read = _read_until_safe_start(stall_url, m3undle_url, timeout_seconds=60.0)

        # The post-reconnect stream never offers an IDR; M3Undle must accumulate enough
        # bytes (default 512 KiB) before it accepts a packet-boundary fallback, so this
        # needs more headroom than the IDR-bearing TS-SAFE-02.
        events = wait_for_event_count(m3undle_url, EVENT_MPEGTS_SAFE_START_SELECTED, minimum=2, timeout_seconds=60)
        safe_start_events = [e for e in events if _event_kind_int(e) == EVENT_MPEGTS_SAFE_START_SELECTED]
        safe_start_count = len(safe_start_events)
        reconnect_count = count_events(events, EVENT_RECONNECT_SCHEDULED)
        second_safe_start_kind = _safe_start_kind(safe_start_events[1]) if safe_start_count >= 2 else None
        data = read_stream_bytes(stall_url, TS_PACKET_SIZE * 3) if safe_start_count >= 2 else b""
        aligned = is_ts_aligned(data)
        reasons: list[str] = []
        add_reason(reasons, safe_start_count >= 2, "reconnect did not record a second safe-start selection")
        add_reason(reasons, reconnect_count >= 1, "diagnostics missed reconnect scheduling")
        add_reason(reasons, second_safe_start_kind == "FallbackPacketBoundary", f"reconnect without an IDR did not fall back to packet-boundary safe start (got {second_safe_start_kind!r})")
        add_reason(reasons, aligned, "late subscriber sample was not TS aligned after fallback safe start")
        print("PASS" if not reasons else "FAIL")
        for reason in reasons:
            print(f"  - {reason}")
        ctx.record(
            "TS-SAFE-03", not reasons, "; ".join(reasons) if reasons else "reconnect without an IDR fell back to packet-boundary safe start",
            {"initial_bytes_read": initial_bytes_read, "safe_start_event_count": safe_start_count, "reconnect_event_count": reconnect_count, "second_safe_start_kind": second_safe_start_kind, "diagnostic_event_kinds": event_kinds(events), "late_subscriber_ts_aligned": aligned, "provider_counts": provider_state(provider_port)},
        )
    except Exception as exc:
        ctx.record("TS-SAFE-03", False, str(exc))
    finally:
        teardown(m3undle_url, provider_id, proc)


def _read_until_safe_start(url: str, m3undle_url: str, *, timeout_seconds: float) -> int:
    """Read chunks from url until a safe-start selection is observed or the
    connection ends, mirroring the frozen script's inline poll-while-reading
    loop for TS-SAFE-02/03."""
    import urllib.request

    total = 0
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout_seconds) as resp:
            while True:
                chunk = resp.read(512)
                if not chunk:
                    break
                total += len(chunk)
                events = get_events(m3undle_url)
                if count_events(events, EVENT_MPEGTS_SAFE_START_SELECTED) >= 1:
                    break
    except Exception:
        pass
    return total


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    collector = _RecordCollector()
    state: dict[str, object] = {"reason": "Stream-safe-start suite setup did not complete", "records": collector.records}
    try:
        scenario_ts_safe_start_basic(collector, base_url)
        scenario_ts_safe_start_reconnect(collector, base_url)
        scenario_ts_safe_start_reconnect_no_idr(collector, base_url)
        state["reason"] = None
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"Stream-safe-start suite setup failed: {exc}"
        return {"state": state}


def _register_case(case_id: str) -> None:
    @SUITE.case(case_id)
    def _replay(ctx, state):
        records: dict[str, tuple[bool | None, str, Any]] = state["records"]  # type: ignore[assignment]
        entry = records.get(case_id)
        if entry is None:
            ctx.skip(case_id, str(state.get("reason") or "Stream-safe-start suite setup did not reach this case"))
            return
        passed, message, detail = entry
        ctx.record(case_id, passed, message, detail)


for _case_id in CASE_IDS:
    _register_case(_case_id)
