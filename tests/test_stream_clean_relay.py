"""Port of the frozen clean-relay suite, from
scripts/srv1/run_stream_clean_relay_scenarios.py.

Validates issue #65 -- provider-scoped FFmpeg clean remux relay for live
MPEG-TS streams: the relay starts and stays TS-aligned, survives a
reconnect, stays off when disabled, suppresses a provider replay via an
in-process FFmpeg reconnect, suppresses a mid-connection unsignaled
timestamp discontinuity, and falls back to a bounded overlap-trim in direct
(no-FFmpeg) mode when the trim can't complete in budget.

Structural adaptation, not a behavior change: same collector-replay pattern
already used for the other four ported stream-scenario suites.
"""

from __future__ import annotations

import hashlib
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.container import get_docker_gateway
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.content_fingerprint import build_source_es, count_resolved, first_backward_jump, resolve_offsets
from m3undle_lab.simulator import SIMULATOR_ENGINE_DIR, SimulatorInstance
from m3undle_lab.stream_scenarios import get_json, iter_chunks_with_deadline, post_json, read_stream_bytes

SUITE = suite("stream-clean-relay", group="core", order=150)
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios" / "core"
DEFAULT_FIXTURE = str(FIXTURES_DIR / "providers" / "provider-clean-relay.json")
CLEAN_RELAY_LOOP_LONG = str(FIXTURES_DIR / "media" / "clean-relay-loop-long.ts")
TIMESTAMP_DISCONTINUITY_FIXTURE = "fixtures/synthetic/clean-relay-timestamp-discontinuity.ts"

EVENT_UPSTREAM_FAILURE = 6
EVENT_RECONNECT_SCHEDULED = 7
EVENT_MPEGTS_SAFE_START_SELECTED = 14
EVENT_FFMPEG_RELAY_STARTED = 16
EVENT_FFMPEG_RELAY_FALLBACK_TO_DIRECT = 17
EVENT_RECOVERY_OUTPUT_RESUMED = 21
EVENT_RECOVERY_OVERLAP_TRIM_ABANDONED = 30
EVENT_IN_PROCESS_RELAY_TIMELINE_REWIND = 31

TIMELINE_EVENT_KINDS = {
    EVENT_UPSTREAM_FAILURE: "UpstreamFailure",
    EVENT_RECONNECT_SCHEDULED: "ReconnectScheduled",
    EVENT_FFMPEG_RELAY_STARTED: "FfmpegRelayStarted",
    EVENT_FFMPEG_RELAY_FALLBACK_TO_DIRECT: "FfmpegRelayFallbackToDirect",
    EVENT_RECOVERY_OUTPUT_RESUMED: "RecoveryOutputResumed",
    EVENT_RECOVERY_OVERLAP_TRIM_ABANDONED: "RecoveryOverlapTrimAbandoned",
    EVENT_IN_PROCESS_RELAY_TIMELINE_REWIND: "InProcessRelayTimelineRewind",
}

TS_PACKET_SIZE = 188
CASE_IDS = ["CLEAN-RELAY-01", "CLEAN-RELAY-02", "CLEAN-RELAY-04", "CLEAN-RELAY-05", "CLEAN-RELAY-06", "CLEAN-RELAY-07"]


def _timestamp_discontinuity_fixture_path() -> str:
    if SIMULATOR_ENGINE_DIR is None:
        raise RuntimeError(
            "M3UNDLE_SIMULATOR_ENGINE_DIR is not set. CLEAN-RELAY-06 needs the public "
            "simulator repo's fixtures/synthetic/ fixture -- point it at a checkout of "
            "Sydney-Elvis/M3Undle-provider-simulator in lab.env."
        )
    return str(SIMULATOR_ENGINE_DIR / TIMESTAMP_DISCONTINUITY_FIXTURE)


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


def event_bytes_suppressed(events: list[dict[str, Any]], kind: int) -> int:
    for event in events:
        if event_kind(event) != kind:
            continue
        raw = event.get("bytesSuppressed") if "bytesSuppressed" in event else event.get("BytesSuppressed")
        if isinstance(raw, int):
            return raw
    return 0


def _event_field(event: dict[str, Any], camel: str, pascal: str) -> Any:
    return event.get(camel) if camel in event else event.get(pascal)


def _parse_event_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def print_event_timeline(events: list[dict[str, Any]], kinds: dict[int, str]) -> None:
    relevant = [e for e in events if event_kind(e) in kinds]
    relevant.sort(key=lambda e: _event_field(e, "timestampUtc", "TimestampUtc") or "")
    if not relevant:
        print("  (no matching diagnostic events)")
        return
    t0 = _parse_event_timestamp(_event_field(relevant[0], "timestampUtc", "TimestampUtc"))
    for event in relevant:
        t1 = _parse_event_timestamp(_event_field(event, "timestampUtc", "TimestampUtc"))
        elapsed = f"+{(t1 - t0).total_seconds():7.3f}s " if t0 is not None and t1 is not None else ""
        kind_name = kinds.get(event_kind(event), str(event_kind(event)))
        print(f"  {elapsed}{kind_name} -- {_event_field(event, 'message', 'Message') or ''}")


def _decode_pes_timestamp(data: bytes) -> int | None:
    if len(data) < 5:
        return None
    if data[0] & 1 != 1 or data[2] & 1 != 1 or data[4] & 1 != 1:
        return None
    return ((data[0] >> 1) & 0x07) << 30 | data[1] << 22 | ((data[2] >> 1) & 0x7F) << 15 | data[3] << 7 | ((data[4] >> 1) & 0x7F)


def extract_video_dts(data: bytes) -> list[int]:
    timestamps: list[int] = []
    for offset in range(0, len(data) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
        packet = data[offset : offset + TS_PACKET_SIZE]
        if packet[0] != 0x47 or packet[1] & 0x40 == 0:
            continue
        adaptation_control = (packet[3] >> 4) & 0x03
        if adaptation_control in {0, 2}:
            continue
        payload_offset = 4
        if adaptation_control == 3:
            payload_offset += 1 + packet[payload_offset]
        payload = packet[payload_offset:]
        if len(payload) < 14 or payload[:3] != b"\x00\x00\x01" or not 0xE0 <= payload[3] <= 0xEF:
            continue
        pts_dts_flags = (payload[7] >> 6) & 0x03
        if pts_dts_flags == 0x03 and len(payload) >= 19:
            timestamp = _decode_pes_timestamp(payload[14:19])
        elif pts_dts_flags == 0x02:
            timestamp = _decode_pes_timestamp(payload[9:14])
        else:
            timestamp = None
        if timestamp is not None:
            timestamps.append(timestamp)
    return timestamps


def dts_is_monotonic(timestamps: list[int]) -> bool:
    wrap = 1 << 33
    half_wrap = wrap >> 1
    for previous, current in zip(timestamps, timestamps[1:]):
        delta = (current - previous + half_wrap) % wrap - half_wrap
        if delta < 0:
            return False
    return True


def get_events(m3undle_url: str) -> list[dict[str, Any]]:
    status, body = get_json(f"{m3undle_url}/status/streams/events", timeout=10)
    if status != 200 or not isinstance(body, list):
        raise RuntimeError(f"events endpoint failed: {status} {body}")
    return [item for item in body if isinstance(item, dict)]


def event_kind(event: dict[str, Any]) -> int | None:
    raw = event.get("kind") if "kind" in event else event.get("Kind")
    return raw if isinstance(raw, int) else None


def event_message(event: dict[str, Any]) -> str:
    raw = event.get("message") if "message" in event else event.get("Message")
    return raw if isinstance(raw, str) else ""


def count_events(events: list[dict[str, Any]], kind: int, message_contains: str | None = None) -> int:
    count = 0
    for event in events:
        if event_kind(event) != kind:
            continue
        if message_contains and message_contains not in event_message(event):
            continue
        count += 1
    return count


def wait_for_event_count(m3undle_url: str, kind: int, minimum: int = 1, timeout_seconds: float = 30.0, message_contains: str | None = None) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    latest: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        latest = get_events(m3undle_url)
        if count_events(latest, kind, message_contains=message_contains) >= minimum:
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
    return [event_kind(event) for event in events]


def provider_state(port: int) -> dict[str, Any]:
    status, body = get_json(f"http://127.0.0.1:{port}/debug/state", timeout=10)
    if status == 200 and isinstance(body, dict):
        return body
    return {"status_code": status, "response": body}


def setup_one_provider(m3undle_url: str, provider_name: str, port: int, clean_relay_mode: str | None, fixture: str = DEFAULT_FIXTURE) -> tuple[SimulatorInstance, str, list[str]]:
    bind, public_host = _simulator_address(port)
    proc = SimulatorInstance(fixture=fixture, port=port, bind=bind, public_host=public_host, suite=f"clean-relay-{provider_name}")
    try:
        proc.start()
        if not proc.wait_healthy():
            raise RuntimeError("provider_sim did not start in time")

        client = M3UndleClient(m3undle_url)
        if not client.setup(playlist_url=proc.playlist_url, provider_name=provider_name, max_concurrent_streams=4, clean_relay_mode=clean_relay_mode):
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


def scenario_clean_relay_basic(ctx: _RecordCollector, m3undle_url: str) -> None:
    print("\n--- Scenario: CLEAN-RELAY-01 clean remux starts and stays TS-aligned ---")
    provider_port = 19681
    proc = None
    provider_id = ""
    try:
        proc, provider_id, urls = setup_one_provider(m3undle_url, "provider-clean-relay-basic", provider_port, clean_relay_mode="remux")
        stream_url = next((u for u in urls if "clean-relay-continuous" in u), urls[0])

        # A larger sample gives the fingerprint analyzer enough windows for a meaningful
        # negative control -- this scenario has no fault at all, so it must show zero
        # backward jumps.
        data = read_stream_bytes(stream_url, TS_PACKET_SIZE * 500)
        events = wait_for_event_count(m3undle_url, EVENT_FFMPEG_RELAY_STARTED, minimum=1, timeout_seconds=20, message_contains="FfmpegCleanRemux")
        safe_events = wait_for_event_count(m3undle_url, EVENT_MPEGTS_SAFE_START_SELECTED, minimum=1, timeout_seconds=20)

        relay_count = count_events(events, EVENT_FFMPEG_RELAY_STARTED, "FfmpegCleanRemux")
        safe_count = count_events(safe_events, EVENT_MPEGTS_SAFE_START_SELECTED)
        aligned = is_ts_aligned(data)
        source_es = build_source_es(CLEAN_RELAY_LOOP_LONG)
        offsets = resolve_offsets(data, source_es)
        backward_jump = first_backward_jump(offsets)
        resolved = count_resolved(offsets)
        reasons: list[str] = []
        add_reason(reasons, relay_count >= 1, "clean remux relay did not start")
        add_reason(reasons, safe_count >= 1, "safe-start selection was not recorded")
        add_reason(reasons, aligned, "stream sample was not TS aligned")
        add_reason(reasons, resolved > 0, "fingerprint analyzer negative control resolved zero windows")
        add_reason(reasons, backward_jump is None, f"fingerprint analyzer false-positived on a fault-free stream: {backward_jump}")
        print("PASS" if not reasons else "FAIL")
        for reason in reasons:
            print(f"  - {reason}")
        ctx.record(
            "CLEAN-RELAY-01", not reasons, "; ".join(reasons) if reasons else "clean remux started, safe-start recorded, sample TS aligned",
            {"ffmpeg_relay_started_count": relay_count, "safe_start_event_count": safe_count, "sample_bytes_read": len(data), "ts_aligned": aligned, "fingerprint_windows_resolved": resolved, "fingerprint_backward_jump": backward_jump, "provider_counts": provider_state(provider_port)},
        )
    except Exception as exc:
        ctx.record("CLEAN-RELAY-01", False, str(exc))
    finally:
        teardown(m3undle_url, provider_id, proc)


def scenario_clean_relay_reconnect(ctx: _RecordCollector, m3undle_url: str) -> None:
    print("\n--- Scenario: CLEAN-RELAY-02 reconnect recovers with remux and safe-start ---")
    provider_port = 19682
    proc = None
    provider_id = ""
    try:
        proc, provider_id, urls = setup_one_provider(m3undle_url, "provider-clean-relay-reconnect", provider_port, clean_relay_mode="remux")
        stream_url = next((u for u in urls if "clean-relay-stall" in u), urls[0])

        import urllib.request

        initial_status = None
        initial_bytes_read = 0
        with urllib.request.urlopen(urllib.request.Request(stream_url), timeout=60.0) as resp:
            initial_status = resp.status
            for chunk in iter_chunks_with_deadline(resp, chunk_size=1024, deadline_seconds=60.0):
                initial_bytes_read += len(chunk)
                events = get_events(m3undle_url)
                if count_events(events, EVENT_RECONNECT_SCHEDULED) >= 1:
                    break

        events = wait_for_event_count(m3undle_url, EVENT_FFMPEG_RELAY_STARTED, minimum=2, timeout_seconds=45, message_contains="FfmpegCleanRemux")
        safe_events = wait_for_event_count(m3undle_url, EVENT_MPEGTS_SAFE_START_SELECTED, minimum=2, timeout_seconds=45)

        data = read_stream_bytes(stream_url, TS_PACKET_SIZE * 5)
        final_events = get_events(m3undle_url)
        relay_count = count_events(events, EVENT_FFMPEG_RELAY_STARTED, "FfmpegCleanRemux")
        safe_count = count_events(safe_events, EVENT_MPEGTS_SAFE_START_SELECTED)
        reconnect_count = count_events(final_events, EVENT_RECONNECT_SCHEDULED)
        aligned = is_ts_aligned(data)
        reasons: list[str] = []
        add_reason(reasons, relay_count >= 2, "reconnect did not start a second clean remux relay")
        add_reason(reasons, safe_count >= 2, "reconnect did not record a second safe-start selection")
        add_reason(reasons, reconnect_count >= 1, "diagnostics missed reconnect scheduling")
        add_reason(reasons, aligned, "recovered stream sample was not TS aligned")
        print("PASS" if not reasons else "FAIL")
        for reason in reasons:
            print(f"  - {reason}")
        ctx.record(
            "CLEAN-RELAY-02", not reasons, "; ".join(reasons) if reasons else "reconnect started a second clean remux relay with a second safe-start",
            {"status_codes": {"initial_stream": initial_status}, "ffmpeg_relay_started_count": relay_count, "safe_start_event_count": safe_count, "reconnect_event_count": reconnect_count, "initial_bytes_read": initial_bytes_read, "sample_bytes_read": len(data), "ts_aligned": aligned, "provider_counts": provider_state(provider_port)},
        )
    except Exception as exc:
        ctx.record("CLEAN-RELAY-02", False, str(exc))
    finally:
        teardown(m3undle_url, provider_id, proc)


def scenario_clean_relay_off(ctx: _RecordCollector, m3undle_url: str) -> None:
    print("\n--- Scenario: CLEAN-RELAY-04 mode off remains direct ---")
    provider_port = 19684
    proc = None
    provider_id = ""
    try:
        proc, provider_id, urls = setup_one_provider(m3undle_url, "provider-clean-relay-off", provider_port, clean_relay_mode="off")
        stream_url = next((u for u in urls if "clean-relay-continuous" in u), urls[0])

        data = read_stream_bytes(stream_url, TS_PACKET_SIZE * 5)
        events = get_events(m3undle_url)
        relay_count = count_events(events, EVENT_FFMPEG_RELAY_STARTED)
        fallback_count = count_events(events, EVENT_FFMPEG_RELAY_FALLBACK_TO_DIRECT)
        aligned = is_ts_aligned(data)
        reasons: list[str] = []
        add_reason(reasons, relay_count == 0, "mode off started a clean relay")
        add_reason(reasons, fallback_count == 0, "mode off recorded a relay fallback")
        add_reason(reasons, aligned, "direct stream sample was not TS aligned")
        print("PASS" if not reasons else "FAIL")
        for reason in reasons:
            print(f"  - {reason}")
        ctx.record(
            "CLEAN-RELAY-04", not reasons, "; ".join(reasons) if reasons else "mode off stayed direct with no relay/fallback events",
            {"ffmpeg_relay_started_count": relay_count, "ffmpeg_relay_fallback_count": fallback_count, "sample_bytes_read": len(data), "ts_aligned": aligned, "provider_counts": provider_state(provider_port)},
        )
    except Exception as exc:
        ctx.record("CLEAN-RELAY-04", False, str(exc))
    finally:
        teardown(m3undle_url, provider_id, proc)


def scenario_clean_relay_inprocess_replay(ctx: _RecordCollector, m3undle_url: str) -> None:
    print("\n--- Scenario: CLEAN-RELAY-05 in-process reconnect suppresses provider replay ---")
    fixture = str(SCENARIOS_DIR / "clean-relay-inprocess-replay.yaml")
    provider_port = 19685
    proc = None
    provider_id = ""
    try:
        proc, provider_id, urls = setup_one_provider(m3undle_url, "provider-clean-relay-inprocess-replay", provider_port, clean_relay_mode="remux", fixture=fixture)
        stream_url = next((u for u in urls if "clean-relay-inprocess-replay" in u), urls[0])

        import urllib.request

        capture = bytearray()
        latest_events: list[dict[str, Any]] = []
        status_code = None
        with urllib.request.urlopen(urllib.request.Request(stream_url), timeout=60.0) as resp:
            status_code = resp.status
            for chunk in iter_chunks_with_deadline(resp, chunk_size=TS_PACKET_SIZE * 35, deadline_seconds=60.0):
                if not chunk:
                    continue
                capture.extend(chunk)
                if len(capture) % (TS_PACKET_SIZE * 350) < len(chunk):
                    # feature/continuous-stream-test's clamped-DTS-ramp recovery path resumes
                    # via a second MpegTsSafeStartSelected -- used here only as a cheap stop
                    # condition, not as the pass/fail signal itself (see reasons below).
                    latest_events = get_events(m3undle_url)
                    if count_events(latest_events, EVENT_MPEGTS_SAFE_START_SELECTED) >= 2:
                        break
                if len(capture) >= 8 * 1024 * 1024:
                    break

        latest_events = get_events(m3undle_url)
        print("  Diagnostic event timeline:")
        print_event_timeline(latest_events, TIMELINE_EVENT_KINDS)
        state = provider_state(provider_port)
        timestamps = extract_video_dts(bytes(capture))
        relay_count = count_events(latest_events, EVENT_FFMPEG_RELAY_STARTED, "FfmpegCleanRemux")
        reconnect_count = count_events(latest_events, EVENT_RECONNECT_SCHEDULED)
        rewind_count = count_events(latest_events, EVENT_IN_PROCESS_RELAY_TIMELINE_REWIND)
        resumed_count = count_events(latest_events, EVENT_RECOVERY_OUTPUT_RESUMED)
        resumed_bytes_suppressed = event_bytes_suppressed(latest_events, EVENT_RECOVERY_OUTPUT_RESUMED)
        total_opened = state.get("total_opened", 0)
        aligned = is_ts_aligned(bytes(capture))
        monotonic = len(timestamps) >= 2 and dts_is_monotonic(timestamps)

        # Content-identity check (the real pass/fail signal): clean remux is -c copy, so
        # downstream video ES bytes are byte-identical to the source fixture regardless
        # of what the muxer did to DTS.
        source_es = build_source_es(CLEAN_RELAY_LOOP_LONG)
        offsets = resolve_offsets(bytes(capture), source_es)
        backward_jump = first_backward_jump(offsets)
        resolved = count_resolved(offsets)

        connection_timeline = state.get("connection_timeline", {})
        timeline_entries = list(connection_timeline.values())
        second_connection_first_chunk_index = timeline_entries[1].get("first_chunk_index") if len(timeline_entries) >= 2 else None

        reasons: list[str] = []
        add_reason(reasons, status_code == 200, "downstream stream did not open successfully")
        add_reason(reasons, isinstance(total_opened, int) and total_opened >= 2, "FFmpeg did not open a second provider HTTP connection")
        add_reason(reasons, relay_count == 1, "M3Undle replaced the FFmpeg relay instead of keeping one process")
        add_reason(reasons, reconnect_count == 0, "M3Undle outer reconnect ran during the in-process FFmpeg reconnect")
        add_reason(reasons, rewind_count >= 1, "in-process timeline rewind was not diagnosed")
        add_reason(reasons, second_connection_first_chunk_index == 0, f"provider's second connection did not actually restart from byte zero (first_chunk_index={second_connection_first_chunk_index})")
        add_reason(reasons, resolved > 0, "fingerprint analyzer resolved zero windows against the source fixture")
        add_reason(reasons, backward_jump is None, f"replayed provider content was published downstream, not suppressed: {backward_jump}")
        add_reason(reasons, resumed_count >= 1 and resumed_bytes_suppressed > 0, "recovery did not resume with a real, non-zero suppressed-byte count (RecoveryOutputResumed)")
        add_reason(reasons, monotonic, f"captured downstream video DTS was not monotonic across the reconnect: {timestamps}")
        add_reason(reasons, aligned, "captured downstream stream was not TS aligned")
        print("PASS" if not reasons else "FAIL")
        for reason in reasons:
            print(f"  - {reason}")
        ctx.record(
            "CLEAN-RELAY-05", not reasons, "; ".join(reasons) if reasons else "in-process FFmpeg reconnect suppressed the provider replay",
            {
                "status_codes": {"stream": status_code}, "provider_connections": total_opened, "ffmpeg_relay_started_count": relay_count,
                "outer_reconnect_event_count": reconnect_count, "inprocess_rewind_event_count": rewind_count,
                "recovery_output_resumed_count": resumed_count, "recovery_output_resumed_bytes_suppressed": resumed_bytes_suppressed,
                "capture_bytes": len(capture), "capture_sha256": hashlib.sha256(capture).hexdigest(),
                "second_connection_first_chunk_index": second_connection_first_chunk_index,
                "fingerprint_windows_resolved": resolved, "fingerprint_backward_jump": backward_jump,
                "downstream_dts_monotonic": monotonic, "ts_aligned": aligned, "provider_counts": state,
            },
        )
    except Exception as exc:
        ctx.record("CLEAN-RELAY-05", False, str(exc))
    finally:
        teardown(m3undle_url, provider_id, proc)


def scenario_clean_relay_timestamp_discontinuity(ctx: _RecordCollector, m3undle_url: str) -> None:
    print("\n--- Scenario: CLEAN-RELAY-06 mid-connection unsignaled timestamp discontinuity ---")
    fixture = str(SCENARIOS_DIR / "clean-relay-timestamp-discontinuity.yaml")
    provider_port = 19686
    proc = None
    provider_id = ""
    try:
        proc, provider_id, urls = setup_one_provider(m3undle_url, "provider-clean-relay-timestamp-discontinuity", provider_port, clean_relay_mode="remux", fixture=fixture)
        stream_url = next((u for u in urls if "clean-relay-timestamp-discontinuity" in u), urls[0])

        import urllib.request

        capture = bytearray()
        latest_events: list[dict[str, Any]] = []
        status_code = None
        with urllib.request.urlopen(urllib.request.Request(stream_url), timeout=60.0) as resp:
            status_code = resp.status
            for chunk in iter_chunks_with_deadline(resp, chunk_size=TS_PACKET_SIZE * 35, deadline_seconds=60.0):
                if not chunk:
                    continue
                capture.extend(chunk)
                if len(capture) % (TS_PACKET_SIZE * 350) < len(chunk):
                    latest_events = get_events(m3undle_url)
                    if count_events(latest_events, EVENT_RECOVERY_OUTPUT_RESUMED) >= 1:
                        break
                if len(capture) >= 1_250_000:
                    break

        latest_events = get_events(m3undle_url)
        print("  Diagnostic event timeline:")
        print_event_timeline(latest_events, TIMELINE_EVENT_KINDS)
        state = provider_state(provider_port)
        relay_count = count_events(latest_events, EVENT_FFMPEG_RELAY_STARTED, "FfmpegCleanRemux")
        reconnect_count = count_events(latest_events, EVENT_RECONNECT_SCHEDULED)
        rewind_count = count_events(latest_events, EVENT_IN_PROCESS_RELAY_TIMELINE_REWIND)
        resumed_count = count_events(latest_events, EVENT_RECOVERY_OUTPUT_RESUMED)
        resumed_bytes_suppressed = event_bytes_suppressed(latest_events, EVENT_RECOVERY_OUTPUT_RESUMED)
        total_opened = state.get("total_opened", 0)
        aligned = is_ts_aligned(bytes(capture))

        source_es = build_source_es(_timestamp_discontinuity_fixture_path())
        offsets = resolve_offsets(bytes(capture), source_es)
        backward_jump = first_backward_jump(offsets)
        resolved = count_resolved(offsets)

        reasons: list[str] = []
        add_reason(reasons, status_code == 200, "downstream stream did not open successfully")
        add_reason(reasons, isinstance(total_opened, int) and total_opened == 1, "provider saw more than one connection -- this scenario has no reconnect trigger")
        add_reason(reasons, relay_count == 1, "M3Undle replaced the FFmpeg relay instead of keeping one process")
        add_reason(reasons, reconnect_count == 0, "M3Undle outer reconnect ran even though this is a single uninterrupted connection")
        add_reason(reasons, rewind_count >= 1, "mid-connection timestamp discontinuity was not diagnosed")
        add_reason(reasons, resumed_count >= 1 and resumed_bytes_suppressed > 0, "recovery did not resume with a real, non-zero suppressed-byte count (RecoveryOutputResumed)")
        add_reason(reasons, resolved > 0, "fingerprint analyzer resolved zero windows against the source fixture")
        add_reason(reasons, backward_jump is None, f"rewound content was published downstream, not suppressed: {backward_jump}")
        add_reason(reasons, aligned, "captured downstream stream was not TS aligned")
        print("PASS" if not reasons else "FAIL")
        for reason in reasons:
            print(f"  - {reason}")
        ctx.record(
            "CLEAN-RELAY-06", not reasons, "; ".join(reasons) if reasons else "mid-connection timestamp discontinuity was suppressed",
            {
                "status_codes": {"stream": status_code}, "provider_connections": total_opened, "ffmpeg_relay_started_count": relay_count,
                "outer_reconnect_event_count": reconnect_count, "inprocess_rewind_event_count": rewind_count,
                "recovery_output_resumed_count": resumed_count, "recovery_output_resumed_bytes_suppressed": resumed_bytes_suppressed,
                "capture_bytes": len(capture), "fingerprint_windows_resolved": resolved, "fingerprint_backward_jump": backward_jump,
                "ts_aligned": aligned, "provider_counts": state,
            },
        )
    except Exception as exc:
        ctx.record("CLEAN-RELAY-06", False, str(exc))
    finally:
        teardown(m3undle_url, provider_id, proc)


def scenario_clean_relay_bounded_fallback(ctx: _RecordCollector, m3undle_url: str) -> None:
    print("\n--- Scenario: CLEAN-RELAY-07 bounded overlap-trim fallback (direct mode, no FFmpeg) ---")
    fixture = str(SCENARIOS_DIR / "clean-relay-bounded-fallback.yaml")
    provider_port = 19687
    proc = None
    provider_id = ""
    try:
        proc, provider_id, urls = setup_one_provider(m3undle_url, "provider-clean-relay-bounded-fallback", provider_port, clean_relay_mode="off", fixture=fixture)
        stream_url = next((u for u in urls if "clean-relay-bounded-fallback" in u), urls[0])

        import urllib.request

        capture = bytearray()
        latest_events: list[dict[str, Any]] = []
        status_code = None
        with urllib.request.urlopen(urllib.request.Request(stream_url), timeout=60.0) as resp:
            status_code = resp.status
            for chunk in iter_chunks_with_deadline(resp, chunk_size=TS_PACKET_SIZE * 35, deadline_seconds=60.0):
                if not chunk:
                    continue
                capture.extend(chunk)
                if len(capture) % (TS_PACKET_SIZE * 350) < len(chunk):
                    latest_events = get_events(m3undle_url)
                    if count_events(latest_events, EVENT_RECOVERY_OUTPUT_RESUMED) >= 1:
                        break
                if len(capture) >= 8 * 1024 * 1024:
                    break

        latest_events = get_events(m3undle_url)
        state = provider_state(provider_port)
        relay_count = count_events(latest_events, EVENT_FFMPEG_RELAY_STARTED, "FfmpegCleanRemux")
        reconnect_count = count_events(latest_events, EVENT_RECONNECT_SCHEDULED)
        abandoned_count = count_events(latest_events, EVENT_RECOVERY_OVERLAP_TRIM_ABANDONED)
        resumed_count = count_events(latest_events, EVENT_RECOVERY_OUTPUT_RESUMED)
        resumed_bytes_suppressed = event_bytes_suppressed(latest_events, EVENT_RECOVERY_OUTPUT_RESUMED)
        total_opened = state.get("total_opened", 0)
        aligned = is_ts_aligned(bytes(capture))

        source_es = build_source_es(CLEAN_RELAY_LOOP_LONG)
        offsets = resolve_offsets(bytes(capture), source_es)
        backward_jump = first_backward_jump(offsets)
        resolved = count_resolved(offsets)

        connection_timeline = state.get("connection_timeline", {})
        timeline_entries = list(connection_timeline.values())
        second_connection_first_chunk_index = timeline_entries[1].get("first_chunk_index") if len(timeline_entries) >= 2 else None

        reasons: list[str] = []
        add_reason(reasons, status_code == 200, "downstream stream did not open successfully")
        add_reason(reasons, isinstance(total_opened, int) and total_opened >= 2, "provider did not see a second connection")
        add_reason(reasons, relay_count == 0, "mode off unexpectedly started a clean remux relay")
        add_reason(reasons, reconnect_count >= 1, "M3Undle's own outer reconnect did not run (expected in direct mode, unlike CLEAN-RELAY-05)")
        add_reason(reasons, second_connection_first_chunk_index == 0, f"provider's second connection did not actually restart from byte zero (first_chunk_index={second_connection_first_chunk_index})")
        add_reason(reasons, abandoned_count >= 1, "RecoveryOverlapTrimAbandoned did not fire -- the trim completed or never started instead of being bounded-fallback abandoned")
        add_reason(reasons, resumed_count >= 1 and resumed_bytes_suppressed > 0, "recovery did not resume after abandonment with a real, non-zero suppressed-byte count (RecoveryOutputResumed)")
        add_reason(reasons, resolved > 0, "fingerprint analyzer resolved zero windows against the source fixture")
        add_reason(reasons, backward_jump is None, f"replayed provider content was published downstream despite the abandoned trim: {backward_jump}")
        add_reason(reasons, aligned, "captured downstream stream was not TS aligned")
        print("PASS" if not reasons else "FAIL")
        for reason in reasons:
            print(f"  - {reason}")
        ctx.record(
            "CLEAN-RELAY-07", not reasons, "; ".join(reasons) if reasons else "bounded overlap-trim fallback abandoned and recovered cleanly",
            {
                "status_codes": {"stream": status_code}, "provider_connections": total_opened, "ffmpeg_relay_started_count": relay_count,
                "outer_reconnect_event_count": reconnect_count, "recovery_overlap_trim_abandoned_count": abandoned_count,
                "recovery_output_resumed_count": resumed_count, "recovery_output_resumed_bytes_suppressed": resumed_bytes_suppressed,
                "capture_bytes": len(capture), "second_connection_first_chunk_index": second_connection_first_chunk_index,
                "fingerprint_windows_resolved": resolved, "fingerprint_backward_jump": backward_jump,
                "ts_aligned": aligned, "provider_counts": state,
            },
        )
    except Exception as exc:
        ctx.record("CLEAN-RELAY-07", False, str(exc))
    finally:
        teardown(m3undle_url, provider_id, proc)


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    collector = _RecordCollector()
    state: dict[str, object] = {"reason": "Stream-clean-relay suite setup did not complete", "records": collector.records}
    try:
        scenario_clean_relay_basic(collector, base_url)
        scenario_clean_relay_reconnect(collector, base_url)
        scenario_clean_relay_off(collector, base_url)
        scenario_clean_relay_inprocess_replay(collector, base_url)
        scenario_clean_relay_timestamp_discontinuity(collector, base_url)
        scenario_clean_relay_bounded_fallback(collector, base_url)
        state["reason"] = None
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"Stream-clean-relay suite setup failed: {exc}"
        return {"state": state}


def _register_case(case_id: str) -> None:
    @SUITE.case(case_id)
    def _replay(ctx, state):
        records: dict[str, tuple[bool | None, str, Any]] = state["records"]  # type: ignore[assignment]
        entry = records.get(case_id)
        if entry is None:
            ctx.skip(case_id, str(state.get("reason") or "Stream-clean-relay suite setup did not reach this case"))
            return
        passed, message, detail = entry
        ctx.record(case_id, passed, message, detail)


for _case_id in CASE_IDS:
    _register_case(_case_id)
