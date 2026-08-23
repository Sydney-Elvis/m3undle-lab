"""Port of the frozen stream-health suite, from
scripts/srv1/run_stream_health_scenarios.py.

Validates the stream-channel health-profile classifier (Stable/Cautious/
Unstable) and its clean-watch decay path, plus the auto-relay decision that
profile drives, using /debug/stream-health/events/seed and
/debug/stream-health/{provider}/{channel}.

Structural adaptation, not a behavior change: same collector-replay pattern
already used for test_profiles.py/test_xtream.py/test_hdhr.py/
test_stream_cooldown.py/test_stream_diagnostics.py.
"""

from __future__ import annotations

import platform
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agent.container import get_docker_gateway
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.simulator import SimulatorInstance
from m3undle_lab.stream_scenarios import get_json, post_json, read_stream_bytes, stream_status

SUITE = suite("stream-health", group="core", order=148)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-stream-health.json"
CHANNEL_NAME = "Stream Health Stable"
PROVIDER_NAME = "provider-stream-health-phase4"
PROVIDER_PORT = 19685
PROFILE_NAMES = {0: "Stable", 1: "Cautious", 2: "Unstable"}

CASE_IDS = [
    "HEALTH-DECAY-UNSTABLE-TO-LESS-DEFENSIVE",
    "HEALTH-CLEAN-WATCH-PERSISTED",
    "HEALTH-DECAY-BELOW-THRESHOLD-NO-RELAX",
    "HEALTH-DECAY-NEW-BAD-EVENT-BLOCKS-RELAX",
    "HEALTH-STABLE-NO-HISTORY-AUTO-DIRECT",
    "HEALTH-CLIENT-ABORT-WHILE-HEALTHY",
]


def _simulator_address() -> tuple[str, str | None]:
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{PROVIDER_PORT}"
    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{PROVIDER_PORT}"
    return "127.0.0.1", None


class _RecordCollector:
    def __init__(self) -> None:
        self.records: dict[str, tuple[bool | None, str, Any]] = {}

    def record(self, name: str, passed: bool | None, message: str, detail: Any = None) -> None:
        self.records[name] = (passed, message, detail)


def add_reason(reasons: list[str], condition: bool, message: str) -> None:
    if not condition:
        reasons.append(message)


def _delete(url: str, timeout: float = 15.0) -> tuple[int | None, Any]:
    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body)
            except ValueError:
                return resp.status, body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, body.decode("utf-8", errors="replace")
    except Exception as exc:
        return None, str(exc)


def health_path(provider_id: str, provider_channel_id: str) -> str:
    return f"/debug/stream-health/{quote(provider_id, safe='')}/{quote(provider_channel_id, safe='')}"


def health_events_path(provider_id: str, provider_channel_id: str) -> str:
    return f"/debug/stream-health/events/{quote(provider_id, safe='')}/{quote(provider_channel_id, safe='')}"


def reset_stream_state(m3undle_url: str) -> None:
    post_json(f"{m3undle_url}/debug/streams/reset")
    post_json(f"{m3undle_url}/debug/strikes/reset")


def clear_health(m3undle_url: str, provider_id: str, provider_channel_id: str) -> dict[str, Any]:
    status, body = _delete(f"{m3undle_url}{health_events_path(provider_id, provider_channel_id)}")
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"health clear failed: {status} {body}")
    return body


def query_health(m3undle_url: str, provider_id: str, provider_channel_id: str) -> dict[str, Any]:
    status, body = get_json(f"{m3undle_url}{health_path(provider_id, provider_channel_id)}")
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"health query failed: {status} {body}")
    return body


def seed_health(m3undle_url: str, provider_id: str, provider_channel_id: str, display_name: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    body = {"providerId": provider_id, "providerChannelId": provider_channel_id, "displayName": display_name, "events": events}
    status, response = post_json(f"{m3undle_url}/debug/stream-health/events/seed", body)
    if status != 200 or not isinstance(response, dict):
        raise RuntimeError(f"health seed failed: {status} {response}")
    return response


def profile_name(health: dict[str, Any]) -> str:
    policy = health.get("recoveryPolicy")
    raw = policy.get("profile") if isinstance(policy, dict) else None
    if isinstance(raw, int):
        return PROFILE_NAMES.get(raw, f"Unknown({raw})")
    if isinstance(raw, str):
        return raw
    return "Unknown"


def recovery_policy(health: dict[str, Any]) -> dict[str, Any]:
    policy = health.get("recoveryPolicy")
    return policy if isinstance(policy, dict) else {}


def relay_decision(health: dict[str, Any]) -> dict[str, Any]:
    decision = health.get("autoRelayDecision")
    return decision if isinstance(decision, dict) else {}


def selected_relay_mode(health: dict[str, Any]) -> str:
    mode = relay_decision(health).get("selectedRelayMode")
    return mode if isinstance(mode, str) else ""


def format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def unstable_bad_events(base_utc: datetime) -> list[dict[str, Any]]:
    # Issue #128: ClientAbortAfterRecovery no longer drives the Unstable classifier (a
    # benign viewer disconnect after a recovery isn't reliable evidence). TsSyncLoss
    # (>=2 events) is one of the remaining upstream-only Unstable conditions.
    return [
        {"eventKind": "RecoveryOutputResumed", "eventUtc": format_utc(base_utc), "safeStartKind": "H264Idr", "relayMode": "Direct", "sessionId": "phase4-seeded-idr-recovery"},
        {"eventKind": "MpegTsSyncLost", "eventUtc": format_utc(base_utc + timedelta(seconds=10)), "tsSyncLoss": True, "relayMode": "Direct", "sessionId": "phase4-seeded-ts-sync-loss-1"},
        {"eventKind": "MpegTsSyncLost", "eventUtc": format_utc(base_utc + timedelta(seconds=20)), "tsSyncLoss": True, "relayMode": "Direct", "sessionId": "phase4-seeded-ts-sync-loss-2"},
    ]


def clean_watch_event(event_utc: datetime, duration_seconds: float) -> dict[str, Any]:
    return {"eventKind": "CleanWatchCompleted", "eventUtc": format_utc(event_utc), "cleanWatchDurationSeconds": duration_seconds, "sessionId": "phase4-seeded-clean-watch"}


def find_channel_identity(m3undle_url: str, display_name: str) -> dict[str, str]:
    status, body = get_json(f"{m3undle_url}/api/v1/channels/?page=1&pageSize=100", timeout=15)
    items = body.get("items") if status == 200 and isinstance(body, dict) else None
    if not isinstance(items, list):
        raise RuntimeError(f"channel list failed: {status} {body}")
    for item in items:
        if not isinstance(item, dict) or item.get("displayName") != display_name:
            continue
        provider_channel_id = str(item.get("providerChannelId") or "")
        stream_key = str(item.get("streamKey") or "")
        if provider_channel_id:
            return {"display_name": display_name, "provider_channel_id": provider_channel_id, "stream_key": stream_key}
    raise RuntimeError(f"channel identity for {display_name!r} not found in {items}")


def find_stream_url(m3undle_url: str, display_name: str) -> str:
    status, body = get_json(f"{m3undle_url}/m3u/m3undle.m3u", timeout=15)
    if status != 200 or not isinstance(body, str):
        raise RuntimeError(f"published M3U failed: {status} {body}")
    lines = body.splitlines()
    for index, line in enumerate(lines[:-1]):
        if display_name.lower() in line.lower() and lines[index + 1].startswith("http"):
            return lines[index + 1].strip()
    raise RuntimeError(f"stream URL for {display_name!r} not found in published M3U")


def transfer_clean_media(stream_url: str, target_bytes: int = 32 * 1024) -> dict[str, Any]:
    data = read_stream_bytes(stream_url, target_bytes, timeout=30.0)
    return {"status_code": 200 if data else None, "bytes_read": len(data)}


def wait_for_clean_watch(m3undle_url: str, provider_id: str, provider_channel_id: str, timeout_seconds: float = 45.0) -> dict[str, Any]:
    import time

    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = query_health(m3undle_url, provider_id, provider_channel_id)
        if latest.get("cleanWatchEvents", 0) >= 1:
            return latest
        time.sleep(1)
    return latest


def scenario_decay_unstable_to_cautious(ctx: _RecordCollector, m3undle_url: str, provider_id: str, channel: dict[str, str]) -> None:
    print("\n--- Scenario: HEALTH-DECAY-UNSTABLE-TO-LESS-DEFENSIVE ---")
    provider_channel_id = channel["provider_channel_id"]
    base_utc = datetime.now(UTC) - timedelta(minutes=45)
    bad_events = unstable_bad_events(base_utc)
    clean_events = [clean_watch_event(base_utc + timedelta(minutes=31), 1800)]
    reasons: list[str] = []
    try:
        reset_stream_state(m3undle_url)
        clear_health(m3undle_url, provider_id, provider_channel_id)
        seed_health(m3undle_url, provider_id, provider_channel_id, channel["display_name"], bad_events)
        health_before = query_health(m3undle_url, provider_id, provider_channel_id)
        seed_health(m3undle_url, provider_id, provider_channel_id, channel["display_name"], clean_events)
        health_after = query_health(m3undle_url, provider_id, provider_channel_id)

        before_policy = recovery_policy(health_before)
        add_reason(reasons, profile_name(health_before) == "Unstable", "bad evidence did not classify channel Unstable")
        add_reason(reasons, selected_relay_mode(health_before) == "FfmpegCleanRemux", "Unstable Auto did not select clean remux")
        add_reason(reasons, before_policy.get("allowPacketBoundaryRecoveryFallback") is False, "Unstable policy did not expose strict packet-boundary fallback disablement")
        add_reason(reasons, health_after.get("cleanWatchEvents") == 1, "threshold clean-watch event was not visible")
        add_reason(reasons, health_after.get("cleanWatchDuration") == "00:30:00", "threshold clean-watch duration was not visible")
        add_reason(reasons, profile_name(health_after) == "Cautious", "clean watch did not decay Unstable exactly one level")
        add_reason(reasons, health_after.get("tsSyncLoss") == 2, "raw bad evidence was not retained")
        add_reason(reasons, health_after.get("idrRecoveryResumes") == 1, "raw IDR recovery evidence was not retained")
        add_reason(reasons, selected_relay_mode(health_after) == "Direct", "relaxed Auto profile did not select direct relay")
    except Exception as exc:
        reasons.append(str(exc))
    finally:
        clear_health(m3undle_url, provider_id, provider_channel_id)

    print("PASS" if not reasons else "FAIL")
    for reason in reasons:
        print(f"  - {reason}")
    ctx.record("HEALTH-DECAY-UNSTABLE-TO-LESS-DEFENSIVE", not reasons, "; ".join(reasons) if reasons else "threshold clean watch decayed Unstable exactly one level")


def scenario_clean_watch_below_threshold(ctx: _RecordCollector, m3undle_url: str, provider_id: str, channel: dict[str, str]) -> None:
    print("\n--- Scenario: HEALTH-DECAY-BELOW-THRESHOLD-NO-RELAX ---")
    provider_channel_id = channel["provider_channel_id"]
    base_utc = datetime.now(UTC) - timedelta(minutes=45)
    bad_events = unstable_bad_events(base_utc)
    clean_events = [clean_watch_event(base_utc + timedelta(minutes=31), 1799)]
    reasons: list[str] = []
    try:
        reset_stream_state(m3undle_url)
        clear_health(m3undle_url, provider_id, provider_channel_id)
        seed_health(m3undle_url, provider_id, provider_channel_id, channel["display_name"], bad_events)
        health_before = query_health(m3undle_url, provider_id, provider_channel_id)
        seed_health(m3undle_url, provider_id, provider_channel_id, channel["display_name"], clean_events)
        health_after = query_health(m3undle_url, provider_id, provider_channel_id)
        add_reason(reasons, profile_name(health_before) == "Unstable", "bad evidence did not classify channel Unstable")
        add_reason(reasons, health_after.get("cleanWatchEvents") == 1, "below-threshold clean watch was not visible")
        add_reason(reasons, profile_name(health_after) == "Unstable", "below-threshold clean watch relaxed profile")
        add_reason(reasons, selected_relay_mode(health_after) == "FfmpegCleanRemux", "Unstable Auto stopped selecting clean remux below threshold")
    except Exception as exc:
        reasons.append(str(exc))
    finally:
        clear_health(m3undle_url, provider_id, provider_channel_id)

    print("PASS" if not reasons else "FAIL")
    for reason in reasons:
        print(f"  - {reason}")
    ctx.record("HEALTH-DECAY-BELOW-THRESHOLD-NO-RELAX", not reasons, "; ".join(reasons) if reasons else "below-threshold clean watch did not relax profile")


def scenario_bad_after_clean_watch(ctx: _RecordCollector, m3undle_url: str, provider_id: str, channel: dict[str, str]) -> None:
    print("\n--- Scenario: HEALTH-DECAY-NEW-BAD-EVENT-BLOCKS-RELAX ---")
    provider_channel_id = channel["provider_channel_id"]
    base_utc = datetime.now(UTC) - timedelta(minutes=45)
    bad_events = unstable_bad_events(base_utc)
    clean_events = [clean_watch_event(base_utc + timedelta(minutes=31), 1800)]
    blocking_events = [{"eventKind": "UpstreamFailure", "eventUtc": format_utc(base_utc + timedelta(minutes=32)), "upstreamFailureKind": "UpstreamUnavailable", "sessionId": "phase4-seeded-new-adverse-event"}]
    reasons: list[str] = []
    try:
        reset_stream_state(m3undle_url)
        clear_health(m3undle_url, provider_id, provider_channel_id)
        seed_health(m3undle_url, provider_id, provider_channel_id, channel["display_name"], bad_events + clean_events)
        health_before = query_health(m3undle_url, provider_id, provider_channel_id)
        seed_health(m3undle_url, provider_id, provider_channel_id, channel["display_name"], blocking_events)
        health_after = query_health(m3undle_url, provider_id, provider_channel_id)
        add_reason(reasons, profile_name(health_before) == "Cautious", "threshold clean watch did not initially relax profile")
        add_reason(reasons, profile_name(health_after) == "Unstable", "new adverse event did not block clean-watch decay")
        add_reason(reasons, health_after.get("cleanWatchEvents") == 0, "pre-adverse clean watch still counted after new bad event")
        add_reason(reasons, health_after.get("upstreamFailures") == 1, "new raw upstream failure was not visible")
        add_reason(reasons, selected_relay_mode(health_after) == "FfmpegCleanRemux", "blocked Auto decay did not select clean remux")
    except Exception as exc:
        reasons.append(str(exc))
    finally:
        clear_health(m3undle_url, provider_id, provider_channel_id)

    print("PASS" if not reasons else "FAIL")
    for reason in reasons:
        print(f"  - {reason}")
    ctx.record("HEALTH-DECAY-NEW-BAD-EVENT-BLOCKS-RELAX", not reasons, "; ".join(reasons) if reasons else "new adverse evidence blocked clean-watch decay")


def scenario_stable_no_history_auto_direct(ctx: _RecordCollector, m3undle_url: str, provider_id: str, channel: dict[str, str]) -> None:
    print("\n--- Scenario: HEALTH-STABLE-NO-HISTORY-AUTO-DIRECT ---")
    provider_channel_id = channel["provider_channel_id"]
    reasons: list[str] = []
    try:
        reset_stream_state(m3undle_url)
        clear_health(m3undle_url, provider_id, provider_channel_id)
        health = query_health(m3undle_url, provider_id, provider_channel_id)
        add_reason(reasons, profile_name(health) == "Stable", "no-history channel did not remain Stable")
        add_reason(reasons, selected_relay_mode(health) == "Direct", "Stable Auto channel did not select direct relay")
    except Exception as exc:
        reasons.append(str(exc))
    finally:
        clear_health(m3undle_url, provider_id, provider_channel_id)

    print("PASS" if not reasons else "FAIL")
    for reason in reasons:
        print(f"  - {reason}")
    ctx.record("HEALTH-STABLE-NO-HISTORY-AUTO-DIRECT", not reasons, "; ".join(reasons) if reasons else "no-history Auto channel stayed direct")


def scenario_clean_watch_persisted(ctx: _RecordCollector, m3undle_url: str, provider_id: str, channel: dict[str, str]) -> None:
    print("\n--- Scenario: HEALTH-CLEAN-WATCH-PERSISTED ---")
    provider_channel_id = channel["provider_channel_id"]
    reasons: list[str] = []
    media_transfer: dict[str, Any] = {}
    try:
        reset_stream_state(m3undle_url)
        clear_health(m3undle_url, provider_id, provider_channel_id)
        media_transfer = transfer_clean_media(channel["stream_url"])
        health_after = wait_for_clean_watch(m3undle_url, provider_id, provider_channel_id)
        add_reason(reasons, media_transfer.get("status_code") == 200, "clean media tune did not return 200")
        add_reason(reasons, media_transfer.get("bytes_read", 0) > 0, "clean media tune transferred no bytes")
        add_reason(reasons, health_after.get("cleanWatchEvents", 0) >= 1, "real clean session did not persist CleanWatchCompleted")
        add_reason(reasons, health_after.get("cleanWatchDuration") not in (None, "00:00:00"), "real clean session duration was not positive")
    except Exception as exc:
        reasons.append(str(exc))
    finally:
        reset_stream_state(m3undle_url)
        clear_health(m3undle_url, provider_id, provider_channel_id)

    print("PASS" if not reasons else "FAIL")
    for reason in reasons:
        print(f"  - {reason}")
    ctx.record("HEALTH-CLEAN-WATCH-PERSISTED", not reasons, "; ".join(reasons) if reasons else "real clean session persisted CleanWatchCompleted", media_transfer)


def scenario_client_abort_while_healthy(ctx: _RecordCollector, m3undle_url: str, provider_id: str, channel: dict[str, str]) -> None:
    print("\n--- Scenario: HEALTH-CLIENT-ABORT-WHILE-HEALTHY ---")
    provider_channel_id = channel["provider_channel_id"]
    reasons: list[str] = []
    try:
        reset_stream_state(m3undle_url)
        clear_health(m3undle_url, provider_id, provider_channel_id)
        query_health(m3undle_url, provider_id, provider_channel_id)

        # Client A tunes in and disconnects well before the fixture's natural end -- a
        # genuine mid-stream client abort, not a graceful completion -- while the
        # upstream itself stays healthy throughout.
        aborted_transfer = transfer_clean_media(channel["stream_url"], target_bytes=4096)
        # Client B tunes the same channel immediately after. If the abort had torn down
        # or degraded the shared upstream session, this would fail or serve empty data.
        survivor_transfer = transfer_clean_media(channel["stream_url"], target_bytes=4096)

        health_after = query_health(m3undle_url, provider_id, provider_channel_id)

        add_reason(reasons, aborted_transfer.get("status_code") == 200, "aborting client's tune-in did not return 200")
        add_reason(reasons, aborted_transfer.get("bytes_read", 0) > 0, "aborting client read no bytes before disconnecting")
        add_reason(reasons, survivor_transfer.get("status_code") == 200, "second client after the abort did not return 200")
        add_reason(reasons, survivor_transfer.get("bytes_read", 0) > 0, "second client after the abort read no bytes")
        add_reason(reasons, profile_name(health_after) == "Stable", "benign client abort degraded the channel's health profile")
        add_reason(reasons, selected_relay_mode(health_after) == "Direct", "benign client abort changed the Auto channel's relay selection")
        add_reason(reasons, health_after.get("tsSyncLoss", 0) == 0, "client abort was recorded as TS sync loss evidence")
    except Exception as exc:
        reasons.append(str(exc))
    finally:
        reset_stream_state(m3undle_url)
        clear_health(m3undle_url, provider_id, provider_channel_id)

    print("PASS" if not reasons else "FAIL")
    for reason in reasons:
        print(f"  - {reason}")
    ctx.record("HEALTH-CLIENT-ABORT-WHILE-HEALTHY", not reasons, "; ".join(reasons) if reasons else "benign client abort did not degrade a healthy upstream's profile")


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    collector = _RecordCollector()
    state: dict[str, object] = {"reason": "Stream-health suite setup did not complete", "records": collector.records}
    proc: SimulatorInstance | None = None
    provider_id = ""

    try:
        # Surface check -- confirm the debug stream-health endpoint exists and exposes
        # the shape scenarios rely on, before spending time on provider setup. Raises
        # naturally into the outer except below (matching the frozen script: this
        # failing aborts the whole suite, same as any other setup failure).
        health = query_health(base_url, "phase4-surface-check", "phase4-surface-check")
        if not isinstance(health.get("recoveryPolicy"), dict) or not isinstance(health.get("autoRelayDecision"), dict):
            raise RuntimeError(f"debug stream-health surface is incomplete: {health}")

        bind, public_host = _simulator_address()
        proc = SimulatorInstance(fixture=FIXTURE, port=PROVIDER_PORT, bind=bind, public_host=public_host, suite="stream-health")
        proc.start()
        if not proc.wait_healthy():
            state["reason"] = "Provider simulator did not become healthy"
            return {"state": state}

        client = M3UndleClient(base_url)
        if not client.setup(playlist_url=proc.playlist_url, provider_name=PROVIDER_NAME, max_concurrent_streams=4, clean_relay_mode="auto"):
            state["reason"] = client.last_setup_error or "Setup sequence failed"
            return {"state": state}
        provider_id = client.provider_id or ""

        channel = find_channel_identity(base_url, CHANNEL_NAME)
        channel["stream_url"] = find_stream_url(base_url, CHANNEL_NAME)

        scenario_decay_unstable_to_cautious(collector, base_url, provider_id, channel)
        scenario_clean_watch_persisted(collector, base_url, provider_id, channel)
        scenario_clean_watch_below_threshold(collector, base_url, provider_id, channel)
        scenario_bad_after_clean_watch(collector, base_url, provider_id, channel)
        scenario_stable_no_history_auto_direct(collector, base_url, provider_id, channel)
        scenario_client_abort_while_healthy(collector, base_url, provider_id, channel)

        state["reason"] = None
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"Stream-health suite setup failed: {exc}"
        return {"state": state}
    finally:
        reset_stream_state(base_url)
        if provider_id:
            M3UndleClient(base_url).delete_provider_with_retry(provider_id)
        if proc is not None:
            proc.stop()


def _register_case(case_id: str) -> None:
    @SUITE.case(case_id)
    def _replay(ctx, state):
        records: dict[str, tuple[bool | None, str, Any]] = state["records"]  # type: ignore[assignment]
        entry = records.get(case_id)
        if entry is None:
            ctx.skip(case_id, str(state.get("reason") or "Stream-health suite setup did not reach this case"))
            return
        passed, message, detail = entry
        ctx.record(case_id, passed, message, detail)


for _case_id in CASE_IDS:
    _register_case(_case_id)
