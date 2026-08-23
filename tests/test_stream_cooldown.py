"""Port of the frozen stream-cooldown suite, from
scripts/srv1/run_stream_cooldown_scenarios.py.

Phase "immediate" covers admission-time cooldowns from upstream 407/429
responses. Phase "outage" is a separate, longer-running exhaustion path for
5xx and transport failures, with a temporary fast-reconnect-timing restart
around it so the outage window doesn't have to be waited out at production
duration.

Structural adaptation, not a behavior change: same collector-replay pattern
already used for test_profiles.py/test_xtream.py/test_hdhr.py --
@SUITE.setup runs the original scenario functions unchanged (ported from the
frozen script almost verbatim, only swapping in this lab's already-verified
SimulatorInstance/M3UndleClient/get_docker_gateway instead of the frozen
script's own bespoke Docker-gateway-detection and provider-setup helpers)
and each registered case replays its own precomputed result.
"""

from __future__ import annotations

import platform
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.container import get_docker_gateway, wait_up
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.commands import CONTAINER_NAME
from m3undle_lab.simulator import SimulatorInstance
from m3undle_lab.stream_scenarios import get_json, post_json, stream_status

SUITE = suite("stream-cooldown", group="core", order=146)
SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios" / "core"

EVENT = {"UpstreamFailure": 6, "ReconnectScheduled": 7, "CooldownRecorded": 9}
FAILURE = {
    "UpstreamServerError": 4, "Transport": 5,
    "UpstreamProxyAuthRequired": 8, "UpstreamRateLimited": 9,
}
FAST_RECONNECT_READ_STALL_SECONDS = 2
FAST_RECONNECT_OUTAGE_WINDOW_SECONDS = 5
STREAMING_SETTINGS_PATH = "/api/v1/settings/streaming"
STREAMING_SETTINGS_FIELDS = (
    "streamingEnabled", "maxConcurrentSessions", "idleGraceSeconds", "idleGraceHardCapSeconds",
    "bufferMaxBytesPerSession", "bufferMaxBytesHardCap", "bufferReadChunkSizeBytes",
    "reconnectReadStallTimeoutSeconds", "reconnectOutageWindowSeconds", "reconnectConnectTimeoutSeconds",
)


@dataclass(frozen=True)
class CooldownScenario:
    scenario_id: str
    name: str
    phase: str
    fixture: str
    provider_name: str
    port: int
    channel_id: str
    upstream_condition: str
    expected_failure_kind: int
    max_cooldown_seconds: int
    provider_retry_after_seconds: int | None = None
    outage_timeout_seconds: int = 150


IMMEDIATE_SCENARIOS = [
    CooldownScenario(
        scenario_id="COOLDOWN-407-NO-HEADER", name="407 no Retry-After cooldown", phase="immediate",
        fixture=str(SCENARIOS_DIR / "cooldown-407-no-retry-after.yaml"), provider_name="provider-cooldown-407",
        port=19107, channel_id="channel-407", upstream_condition="HTTP 407 without Retry-After",
        expected_failure_kind=FAILURE["UpstreamProxyAuthRequired"], max_cooldown_seconds=30,
    ),
    CooldownScenario(
        scenario_id="COOLDOWN-429-NO-HEADER", name="429 fallback cooldown", phase="immediate",
        fixture=str(SCENARIOS_DIR / "cooldown-429-no-retry-after.yaml"), provider_name="provider-cooldown-429-no-retry-after",
        port=19428, channel_id="channel-429-no-retry-after", upstream_condition="HTTP 429 without Retry-After",
        expected_failure_kind=FAILURE["UpstreamRateLimited"], max_cooldown_seconds=60,
    ),
    CooldownScenario(
        scenario_id="COOLDOWN-429-RETRY-AFTER", name="429 Retry-After override", phase="immediate",
        fixture=str(SCENARIOS_DIR / "cooldown-429-retry-after.yaml"), provider_name="provider-cooldown-429",
        port=19429, channel_id="channel-429", upstream_condition="HTTP 429 with Retry-After: 20",
        expected_failure_kind=FAILURE["UpstreamRateLimited"], max_cooldown_seconds=20, provider_retry_after_seconds=20,
    ),
]

OUTAGE_SCENARIOS = [
    CooldownScenario(
        scenario_id="COOLDOWN-5XX-NO-HEADER", name="503 outage exhaustion", phase="outage",
        fixture=str(SCENARIOS_DIR / "cooldown-503-no-retry-after.yaml"), provider_name="provider-cooldown-503-no-retry-after",
        port=19503, channel_id="channel-503-no-retry-after", upstream_condition="HTTP 503 without Retry-After",
        expected_failure_kind=FAILURE["UpstreamServerError"], max_cooldown_seconds=30,
    ),
    CooldownScenario(
        scenario_id="COOLDOWN-TRANSPORT", name="transport outage exhaustion", phase="outage",
        fixture=str(SCENARIOS_DIR / "cooldown-transport.yaml"), provider_name="provider-cooldown-transport",
        port=19505, channel_id="channel-transport", upstream_condition="disconnect before response headers",
        expected_failure_kind=FAILURE["Transport"], max_cooldown_seconds=15,
    ),
]

ALL_SCENARIOS = [*IMMEDIATE_SCENARIOS, *OUTAGE_SCENARIOS]


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


# ---------------------------------------------------------------------------
# Helpers (ported unchanged, except get_json/post_json come from
# m3undle_lab.stream_scenarios rather than a sibling script import)
# ---------------------------------------------------------------------------

def provider_state(port: int) -> dict[str, Any]:
    status, body = get_json(f"http://127.0.0.1:{port}/debug/state", timeout=10)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"provider state unavailable on port {port}: {status} {body}")
    return body


def reset_provider(port: int) -> None:
    status, body = post_json(f"http://127.0.0.1:{port}/debug/reset", timeout=10)
    if status != 200:
        raise RuntimeError(f"provider reset failed on port {port}: {status} {body}")


def set_channel_healthy(port: int, channel_id: str) -> dict[str, Any]:
    status, body = post_json(
        f"http://127.0.0.1:{port}/debug/channels/{channel_id}/override",
        {"failure_mode": "none", "failure_status_code": 0, "retry_after_seconds": 0},
        timeout=10,
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"provider override failed for {channel_id}: {status} {body}")
    return body


def field(payload: dict[str, Any], name: str) -> Any:
    return payload.get(name) if name in payload else payload.get(name[:1].upper() + name[1:])


def streaming_settings_body(saved: dict[str, Any]) -> dict[str, Any]:
    body = {name: field(saved, name) for name in STREAMING_SETTINGS_FIELDS}
    missing = [name for name, value in body.items() if value is None]
    if missing:
        raise RuntimeError(f"streaming settings response missed fields: {', '.join(missing)}")
    return body


def reconnect_timing(settings_state: dict[str, Any], snapshot_name: str) -> dict[str, Any]:
    snapshot = field(settings_state, snapshot_name)
    if not isinstance(snapshot, dict):
        return {}
    return {
        "read_stall_timeout_seconds": field(snapshot, "reconnectReadStallTimeoutSeconds"),
        "outage_window_seconds": field(snapshot, "reconnectOutageWindowSeconds"),
        "connect_timeout_seconds": field(snapshot, "reconnectConnectTimeoutSeconds"),
    }


def restart_m3undle(base_url: str) -> tuple[bool, dict[str, Any]]:
    result = subprocess.run(["docker", "restart", CONTAINER_NAME], capture_output=True, text=True, timeout=45, check=False)
    observed = {"container": CONTAINER_NAME, "exit_code": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    if result.returncode != 0:
        return False, observed
    ready = wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health"), timeout=90.0)
    observed["health_ready"] = ready
    return ready, observed


def prepare_outage_timing(m3undle_url: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    url = f"{m3undle_url}{STREAMING_SETTINGS_PATH}"
    control: dict[str, Any] = {
        "strategy": "long-running-outage",
        "settings_endpoint": STREAMING_SETTINGS_PATH,
        "requested_reconnect_timing": {
            "read_stall_timeout_seconds": FAST_RECONNECT_READ_STALL_SECONDS,
            "outage_window_seconds": FAST_RECONNECT_OUTAGE_WINDOW_SECONDS,
        },
    }

    status, before = get_json(url, timeout=15)
    control["settings_get_status"] = status
    if status != 200 or not isinstance(before, dict):
        control["fallback_reason"] = "streaming settings API unavailable"
        control["settings_get_response"] = before
        return control, None

    control["before"] = {
        "saved": reconnect_timing(before, "saved"), "applied": reconnect_timing(before, "applied"),
        "restart_required": field(before, "restartRequired"),
    }
    saved = field(before, "saved")
    if not isinstance(saved, dict):
        control["fallback_reason"] = "streaming settings API omitted saved settings"
        return control, None

    restore_body = streaming_settings_body(saved)
    fast_body = dict(restore_body)
    fast_body["reconnectReadStallTimeoutSeconds"] = FAST_RECONNECT_READ_STALL_SECONDS
    fast_body["reconnectOutageWindowSeconds"] = FAST_RECONNECT_OUTAGE_WINDOW_SECONDS

    status, updated = _put_json(url, fast_body)
    control["settings_put_status"] = status
    if status != 200 or not isinstance(updated, dict):
        control["fallback_reason"] = "streaming settings API rejected fast reconnect timing"
        control["settings_put_response"] = updated
        return control, None

    control["after_update"] = {
        "saved": reconnect_timing(updated, "saved"), "applied": reconnect_timing(updated, "applied"),
        "restart_required": field(updated, "restartRequired"),
    }
    restarted, restart_observed = restart_m3undle(m3undle_url)
    control["apply_restart"] = restart_observed
    if not restarted:
        control["fallback_reason"] = "M3Undle did not return after fast reconnect restart"
        return control, restore_body

    status, after_restart = get_json(url, timeout=15)
    control["after_restart_status"] = status
    if status != 200 or not isinstance(after_restart, dict):
        control["fallback_reason"] = "streaming settings API unavailable after fast reconnect restart"
        control["after_restart_response"] = after_restart
        return control, restore_body

    effective = reconnect_timing(after_restart, "applied")
    control["after_restart"] = {
        "saved": reconnect_timing(after_restart, "saved"), "applied": effective,
        "restart_required": field(after_restart, "restartRequired"),
    }
    control["effective_reconnect_timing"] = effective
    if (
        effective.get("read_stall_timeout_seconds") != FAST_RECONNECT_READ_STALL_SECONDS
        or effective.get("outage_window_seconds") != FAST_RECONNECT_OUTAGE_WINDOW_SECONDS
    ):
        control["fallback_reason"] = "fast reconnect timing was not applied after restart"
        return control, restore_body

    control["strategy"] = "settings-api-fast-reconnect"
    return control, restore_body


def _put_json(url: str, body: Any) -> tuple[int | None, Any]:
    import json
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=15.0) as resp:
            response_body = resp.read()
            try:
                return resp.status, json.loads(response_body)
            except ValueError:
                return resp.status, response_body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        response_body = exc.read()
        try:
            return exc.code, json.loads(response_body)
        except ValueError:
            return exc.code, response_body.decode("utf-8", errors="replace")
    except Exception as exc:
        return None, str(exc)


def restore_outage_timing(m3undle_url: str, restore_body: dict[str, Any] | None, control: dict[str, Any]) -> bool:
    if restore_body is None:
        control["restore"] = {"status": "not-needed"}
        return True

    url = f"{m3undle_url}{STREAMING_SETTINGS_PATH}"
    status, updated = _put_json(url, restore_body)
    restore: dict[str, Any] = {
        "settings_put_status": status,
        "after_update": updated if not isinstance(updated, dict) else {
            "saved": reconnect_timing(updated, "saved"), "applied": reconnect_timing(updated, "applied"),
            "restart_required": field(updated, "restartRequired"),
        },
    }
    control["restore"] = restore
    if status != 200:
        restore["status"] = "fail"
        return False

    restarted, restart_observed = restart_m3undle(m3undle_url)
    restore["restart"] = restart_observed
    if not restarted:
        restore["status"] = "fail"
        return False

    status, restored = get_json(url, timeout=15)
    restore["settings_get_status"] = status
    if status != 200 or not isinstance(restored, dict):
        restore["status"] = "fail"
        restore["settings_get_response"] = restored
        return False

    restore["after_restart"] = {
        "saved": reconnect_timing(restored, "saved"), "applied": reconnect_timing(restored, "applied"),
        "restart_required": field(restored, "restartRequired"),
    }
    restore["status"] = "pass"
    return True


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


def event_number(value: Any) -> Any:
    if isinstance(value, str):
        return EVENT.get(value, value)
    return value


def event_kinds(events: list[dict[str, Any]]) -> list[Any]:
    return [event_number(field(event, "kind")) for event in events]


def active_cooldowns(rca: dict[str, Any]) -> list[dict[str, Any]]:
    raw = field(rca, "activeCooldowns")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def wait_for_cooldown(m3undle_url: str, timeout_seconds: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = get_rca(m3undle_url)
        cooldowns = active_cooldowns(latest)
        if cooldowns:
            return latest, cooldowns
        time.sleep(0.5)
    return latest, active_cooldowns(latest)


def wait_for_cooldown_clear(m3undle_url: str, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not active_cooldowns(get_rca(m3undle_url)):
            return True
        time.sleep(0.5)
    return False


def has_event(events: list[dict[str, Any]], event_kind: int) -> bool:
    return any(event_number(field(event, "kind")) == event_kind for event in events)


def failure_kind_matches(events: list[dict[str, Any]], expected: int) -> bool:
    for event in events:
        if event_number(field(event, "kind")) != EVENT["UpstreamFailure"]:
            continue
        failure = field(event, "upstreamFailureKind")
        if failure == expected:
            return True
        if isinstance(failure, str) and FAILURE.get(failure) == expected:
            return True
    return False


def cooldown_remaining_seconds(cooldowns: list[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    for cooldown in cooldowns:
        remaining = field(cooldown, "remainingSeconds")
        if isinstance(remaining, (int, float)):
            values.append(float(remaining))
    return values


def add_reason(reasons: list[str], condition: bool, message: str) -> None:
    if not condition:
        reasons.append(message)


def retry_after_values_present(values: list[str]) -> bool:
    return all(value.strip() for value in values)


def run_immediate_assertions(ctx: _RecordCollector, scenario: CooldownScenario, stream_url: str, m3undle_url: str) -> None:
    print(f"\n--- Scenario: {scenario.scenario_id} {scenario.name} ---")
    reset_provider(scenario.port)

    first_status, first_retry_after = stream_status(stream_url)
    print(f"First request: status={first_status} retry_after={first_retry_after!r}")

    time.sleep(1.0)
    state_after_first = provider_state(scenario.port)
    opened_after_first = int(state_after_first.get("total_opened", -1))
    rca, cooldowns = wait_for_cooldown(m3undle_url, timeout_seconds=15)
    events = get_events(m3undle_url)

    repeated = [stream_status(stream_url) for _ in range(3)]
    for _ in repeated:
        time.sleep(0.3)
    state_after_repeated = provider_state(scenario.port)
    opened_after_repeated = int(state_after_repeated.get("total_opened", -1))

    override_state = set_channel_healthy(scenario.port, scenario.channel_id)
    cooldown_cleared = wait_for_cooldown_clear(m3undle_url, scenario.max_cooldown_seconds + 15)
    recovered_status, recovered_retry_after = stream_status(stream_url)
    state_after_recovery = provider_state(scenario.port)
    opened_after_recovery = int(state_after_recovery.get("total_opened", -1))

    retry_after_values = [first_retry_after, *[retry for _, retry in repeated]]
    remaining = cooldown_remaining_seconds(cooldowns)
    reasons: list[str] = []
    add_reason(reasons, first_status == 503, "first request did not return downstream 503")
    add_reason(reasons, opened_after_first == 1, "first request did not open exactly one upstream stream")
    add_reason(reasons, all(status == 503 for status, _ in repeated), "cooldown retries did not all return 503")
    add_reason(reasons, retry_after_values_present(retry_after_values), "downstream cooldown response missed Retry-After")
    add_reason(reasons, opened_after_repeated == opened_after_first, "cooldown retries reopened provider streams")
    add_reason(reasons, bool(cooldowns), "RCA did not expose an active cooldown")
    add_reason(reasons, bool(remaining) and all(0 < value <= scenario.max_cooldown_seconds + 1 for value in remaining), f"active cooldown did not fit 0..{scenario.max_cooldown_seconds}s class")
    add_reason(reasons, failure_kind_matches(events, scenario.expected_failure_kind), "diagnostics missed expected failure kind")
    add_reason(reasons, has_event(events, EVENT["CooldownRecorded"]), "diagnostics missed CooldownRecorded")
    add_reason(reasons, cooldown_cleared, "same-channel cooldown did not clear before recovery check")
    add_reason(reasons, recovered_status == 200, "same channel was not admitted after healthy override")
    add_reason(reasons, opened_after_recovery > opened_after_repeated, "recovery did not open a new provider stream")
    if scenario.provider_retry_after_seconds is not None:
        try:
            first_retry = int(first_retry_after)
        except ValueError:
            first_retry = 0
        add_reason(reasons, 1 <= first_retry <= scenario.provider_retry_after_seconds, f"provider Retry-After was not honored within 1..{scenario.provider_retry_after_seconds}s")

    print(f"Repeated requests: {repeated}")
    print("PASS" if not reasons else "FAIL")
    for reason in reasons:
        print(f"  - {reason}")

    ctx.record(
        scenario.scenario_id, not reasons,
        "; ".join(reasons) if reasons else "all cooldown/recovery assertions passed",
        {
            "status_codes": {"first": first_status, "repeated": [status for status, _ in repeated], "recovered": recovered_status},
            "diagnostic_event_kinds": event_kinds(events),
            "active_cooldowns": cooldowns,
            "cooldown_remaining_seconds": remaining,
            "healthy_override": override_state,
            "rca_generated_utc": field(rca, "generatedUtc"),
        },
    )


def run_outage_assertions(ctx: _RecordCollector, scenario: CooldownScenario, stream_url: str, m3undle_url: str, effective_reconnect_timing: dict[str, Any]) -> None:
    print(f"\n--- Scenario: {scenario.scenario_id} {scenario.name} ---")
    print(f"Waiting up to {scenario.outage_timeout_seconds}s for outage exhaustion...")
    reset_provider(scenario.port)

    outage_started = time.monotonic()
    first_status, first_retry_after = stream_status(stream_url, read_timeout_seconds=scenario.outage_timeout_seconds)
    first_request_elapsed_seconds = time.monotonic() - outage_started
    rca, cooldowns = wait_for_cooldown(m3undle_url, timeout_seconds=scenario.outage_timeout_seconds)
    cooldown_elapsed_seconds = time.monotonic() - outage_started
    events = get_events(m3undle_url)
    state_before_repeated = provider_state(scenario.port)
    opened_before_repeated = int(state_before_repeated.get("total_opened", -1))

    repeated = [stream_status(stream_url) for _ in range(3)]
    state_after_repeated = provider_state(scenario.port)
    opened_after_repeated = int(state_after_repeated.get("total_opened", -1))

    override_state = set_channel_healthy(scenario.port, scenario.channel_id)
    cooldown_cleared = wait_for_cooldown_clear(m3undle_url, scenario.max_cooldown_seconds + 15)
    recovered_status, recovered_retry_after = stream_status(stream_url)
    state_after_recovery = provider_state(scenario.port)
    opened_after_recovery = int(state_after_recovery.get("total_opened", -1))

    remaining = cooldown_remaining_seconds(cooldowns)
    outage_window_seconds = effective_reconnect_timing.get("outage_window_seconds")
    reasons: list[str] = []
    add_reason(reasons, bool(cooldowns), "outage path never exposed an active cooldown")
    add_reason(reasons, has_event(events, EVENT["ReconnectScheduled"]), "diagnostics missed reconnect activity")
    add_reason(reasons, has_event(events, EVENT["CooldownRecorded"]), "diagnostics missed CooldownRecorded")
    add_reason(reasons, failure_kind_matches(events, scenario.expected_failure_kind), "diagnostics missed expected failure kind")
    add_reason(reasons, bool(remaining) and all(0 < value <= scenario.max_cooldown_seconds + 1 for value in remaining), f"outage cooldown did not fit 0..{scenario.max_cooldown_seconds}s class")
    add_reason(reasons, opened_before_repeated > 1, "initial outage path did not reopen upstream before cooldown")
    if isinstance(outage_window_seconds, (int, float)):
        add_reason(reasons, cooldown_elapsed_seconds >= max(0.0, float(outage_window_seconds) - 0.5), "cooldown was recorded before the effective outage window elapsed")
    add_reason(reasons, all(status == 503 for status, _ in repeated), "cooldown retries did not all return 503")
    add_reason(reasons, retry_after_values_present([retry for _, retry in repeated]), "cooldown retries missed Retry-After")
    add_reason(reasons, opened_after_repeated == opened_before_repeated, "cooldown retries reopened provider streams")
    add_reason(reasons, cooldown_cleared, "same-channel outage cooldown did not clear before recovery check")
    add_reason(reasons, recovered_status == 200, "same channel was not admitted after healthy override")
    add_reason(reasons, opened_after_recovery > opened_after_repeated, "recovery did not open a new provider stream")

    print(f"Initial request after outage path: status={first_status} retry_after={first_retry_after!r}")
    print(f"Repeated requests: {repeated}")
    print("PASS" if not reasons else "FAIL")
    for reason in reasons:
        print(f"  - {reason}")

    ctx.record(
        scenario.scenario_id, not reasons,
        "; ".join(reasons) if reasons else "all outage/recovery assertions passed",
        {
            "outage_elapsed_seconds": {"first_request": round(first_request_elapsed_seconds, 3), "cooldown_observed": round(cooldown_elapsed_seconds, 3)},
            "status_codes": {"first": first_status, "repeated": [status for status, _ in repeated], "recovered": recovered_status},
            "diagnostic_event_kinds": event_kinds(events),
            "active_cooldowns": cooldowns,
            "cooldown_remaining_seconds": remaining,
            "healthy_override": override_state,
            "rca_generated_utc": field(rca, "generatedUtc"),
        },
    )


def stream_url_for_channel(stream_urls: list[str], channel_id: str) -> str:
    stream_url = next((url for url in stream_urls if channel_id in url), None)
    if stream_url is None:
        raise RuntimeError(f"stream url for channel {channel_id!r} not found in {stream_urls}")
    return stream_url


def run_scenario(ctx: _RecordCollector, m3undle_url: str, scenario: CooldownScenario, effective_reconnect_timing: dict[str, Any]) -> None:
    simulator = None
    client = M3UndleClient(m3undle_url)
    try:
        bind, public_host = _simulator_address(scenario.port)
        simulator = SimulatorInstance(fixture=scenario.fixture, port=scenario.port, bind=bind, public_host=public_host, suite=f"cooldown-{scenario.scenario_id.lower()}")
        simulator.start()
        if not simulator.wait_healthy():
            raise RuntimeError("provider_sim did not start in time")

        if not client.setup(playlist_url=simulator.playlist_url, provider_name=scenario.provider_name, max_concurrent_streams=4):
            raise RuntimeError(client.last_setup_error or "provider setup failed")

        client.reset_debug_state()
        stream_urls = client.get_stream_urls()
        stream_url = stream_url_for_channel(stream_urls, scenario.channel_id)
        if scenario.phase == "outage":
            run_outage_assertions(ctx, scenario, stream_url, m3undle_url, effective_reconnect_timing)
        else:
            run_immediate_assertions(ctx, scenario, stream_url, m3undle_url)
    except Exception as exc:
        print(f"FAIL: {scenario.scenario_id}: {exc}")
        ctx.record(scenario.scenario_id, False, str(exc))
    finally:
        client.reset_debug_state()
        if client.provider_id:
            client.delete_provider_with_retry(client.provider_id)
        if simulator is not None:
            simulator.stop()


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    collector = _RecordCollector()
    state: dict[str, object] = {"reason": "Stream-cooldown suite setup did not complete", "records": collector.records}

    outage_timing: dict[str, Any] = {"strategy": "not-needed", "restore": {"status": "not-needed"}}
    restore_body: dict[str, Any] | None = None
    try:
        outage_timing, restore_body = prepare_outage_timing(base_url)
        effective_reconnect_timing = outage_timing.get("effective_reconnect_timing")
        if not isinstance(effective_reconnect_timing, dict):
            effective_reconnect_timing = {}

        for scenario in ALL_SCENARIOS:
            run_scenario(collector, base_url, scenario, effective_reconnect_timing)

        state["reason"] = None
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"Stream-cooldown suite setup failed: {exc}"
        return {"state": state}
    finally:
        restore_outage_timing(base_url, restore_body, outage_timing)


def _register_case(case_id: str) -> None:
    @SUITE.case(case_id)
    def _replay(ctx, state):
        records: dict[str, tuple[bool | None, str, Any]] = state["records"]  # type: ignore[assignment]
        entry = records.get(case_id)
        if entry is None:
            ctx.skip(case_id, str(state.get("reason") or "Stream-cooldown suite setup did not reach this case"))
            return
        passed, message, detail = entry
        ctx.record(case_id, passed, message, detail)


for _scenario in ALL_SCENARIOS:
    _register_case(_scenario.scenario_id)
