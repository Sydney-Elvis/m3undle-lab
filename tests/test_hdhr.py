"""Port of the frozen HDHomeRun device and tuner suite (HDHR-01..12, HDHR-TS-01,
HDHR-OVERRIDE-01, HDHR-RELAY-01, HDHR-UNENFORCED-01, HDHR-RST-01).

No simulator content changes needed -- these are API + HTTP tests against
M3Undle's HDHR compatibility surface: device/lineup discovery, tuning by
vchannel and auto-tuner, tuner retune/capacity limits, legacy path parity,
real hdhomerun_config discovery (skipped if the binary isn't installed),
MPEG-TS framing on tune routes, guide-number overrides surviving a rebuild
and a container restart, auto-relay selecting FfmpegCleanRemux instead of
silently falling back to Direct for an Unstable channel, and unenforced
tuner-count concurrency.

Structural adaptation, not a behavior change: same as test_profiles.py/
test_xtream.py -- the frozen script is one long imperative main() with an
asymmetric per-path recording pattern agent.suites' "every declared case
records exactly once" model can't represent. @SUITE.setup runs the exact
same sequence (test functions ported unchanged, called against a
_RecordCollector standing in for the original RunContext) and stores each
one's outcome; each registered case replays its own precomputed result.

Also fixes a real drift from the container-name rename this migration
already made: the frozen script's restart helper ran `docker restart
m3undle` -- the old lab's container name. This lab's container is
CONTAINER_NAME ("m3undle-lab"), imported from m3undle_lab.commands rather
than hardcoded a second time.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from agent import common as lab_common
from agent.container import get_docker_gateway, wait_up
from agent.suites import suite

from m3undle_lab.api import HoldOpenStream, M3UndleClient
from m3undle_lab.commands import CONTAINER_NAME
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("hdhr", group="core", order=125)
SIM_PORT = 19013
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-a.json"
MIN_STREAM_BYTES = 1024

CASE_IDS = [
    "HDHR-01", "HDHR-02", "HDHR-03", "HDHR-04", "HDHR-05", "HDHR-06", "HDHR-07",
    "HDHR-08", "HDHR-09", "HDHR-10", "HDHR-11", "HDHR-12",
    "HDHR-TS-01", "HDHR-OVERRIDE-01", "HDHR-RELAY-01", "HDHR-UNENFORCED-01", "HDHR-RST-01",
]


class _RecordCollector:
    def __init__(self) -> None:
        self.records: dict[str, tuple[bool | None, str, Any]] = {}

    def record(self, name: str, passed: bool | None, message: str, detail: Any = None) -> None:
        self.records[name] = (passed, message, detail)


def _simulator_address() -> tuple[str, str | None]:
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{SIM_PORT}"
    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{SIM_PORT}"
    return "127.0.0.1", None


# ---------------------------------------------------------------------------
# Helpers (ported unchanged)
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: float = 10.0) -> tuple[int, object]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {}
    except Exception as exc:
        return 0, {"error": str(exc)}


def _read_stream_bytes(url: str, num_bytes: int = MIN_STREAM_BYTES, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(num_bytes)
    except Exception:
        return b""


def _read_stream_status(url: str, num_bytes: int = 1, timeout: float = 10.0) -> tuple[int, int, str]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(num_bytes)
            return resp.status, len(data), ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return exc.code, 0, body
    except Exception as exc:
        return 0, 0, str(exc)


def _get_first_channel(base: str) -> tuple[str, str]:
    """Return (providerChannelId, displayName) of the first active channel."""
    url = f"{base}/api/v1/channels/?page=1&pageSize=10"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            body = json.loads(resp.read())
            items = body.get("items", []) if isinstance(body, dict) else []
            if items:
                first = items[0]
                return str(first.get("providerChannelId", "")), str(first.get("displayName", ""))
    except Exception:
        pass
    return "", ""


def _patch_channel_number(base: str, provider_channel_id: str, channel_number: str | None) -> bool:
    """Apply (or clear when channel_number is None) a guide-number override."""
    if not provider_channel_id:
        return False
    encoded_id = urllib.parse.quote(provider_channel_id, safe="")
    url = f"{base}/api/v1/channels/{encoded_id}"
    body_data: dict = {"clearChannelNumber": True} if channel_number is None else {"channelNumber": int(channel_number), "clearChannelNumber": False}
    data = json.dumps(body_data).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return resp.status in (200, 204)
    except urllib.error.HTTPError as exc:
        return exc.code in (200, 204)
    except Exception:
        return False


def _restart_container_and_wait(base: str, *, timeout: float = 90.0) -> bool:
    """Issue `docker restart <CONTAINER_NAME>` and wait for the service to become live."""
    print(f"  docker restart {CONTAINER_NAME}...", flush=True)
    result = subprocess.run(["docker", "restart", CONTAINER_NAME], capture_output=True, text=True, timeout=30.0, check=False)
    if result.returncode != 0:
        print(f"  docker restart failed: {result.stderr.strip()[:120]}", flush=True)
        return False
    print("  Waiting for container to come live...", flush=True)
    return wait_up(base, CONTAINER_NAME, health_paths=("/livez", "/health"), timeout=timeout)


# ---------------------------------------------------------------------------
# Test functions (ported unchanged -- collector duck-types RunContext.record)
# ---------------------------------------------------------------------------

def _test_hdhr_01(ctx: _RecordCollector, base: str) -> None:
    """HDHR-01: /hdhr/discover.json returns valid device identity."""
    status, body = _get_json(f"{base}/hdhr/discover.json")
    if status != 200 or not isinstance(body, dict):
        ctx.record("HDHR-01", False, f"status={status}")
        return
    ctx.record(
        "HDHR-01", "TunerCount" in body and "DeviceID" in body and "BaseURL" in body,
        f"TunerCount={body.get('TunerCount')} DeviceID={body.get('DeviceID')} BaseURL={body.get('BaseURL')}",
    )


def _test_hdhr_02(ctx: _RecordCollector, base: str) -> None:
    """HDHR-02: /hdhr/lineup.json returns channels with GuideNumber and URL."""
    status, body = _get_json(f"{base}/hdhr/lineup.json")
    if status != 200 or not isinstance(body, list) or not body:
        ctx.record("HDHR-02", False, f"status={status} body={str(body)[:80]}")
        return
    first = body[0] if isinstance(body[0], dict) else {}
    ctx.record(
        "HDHR-02", "GuideNumber" in first and "URL" in first,
        f"channels={len(body)} first GuideNumber={first.get('GuideNumber')} URL={str(first.get('URL'))[:60]}",
    )


def _test_hdhr_03(ctx: _RecordCollector, base: str, expected_channel_count: int) -> None:
    """HDHR-03: /hdhr/lineup_status.json shows ScanInProgress=0 and correct count."""
    status, body = _get_json(f"{base}/hdhr/lineup_status.json")
    if status != 200 or not isinstance(body, dict):
        ctx.record("HDHR-03", False, f"status={status}")
        return
    scan_in_progress = body.get("ScanInProgress", -1)
    ctx.record("HDHR-03", scan_in_progress == 0, f"ScanInProgress={scan_in_progress} ChannelCount={body.get('ChannelCount')} expected_channels={expected_channel_count}")


def _test_hdhr_04(ctx: _RecordCollector, base: str) -> None:
    """HDHR-04: HDHR lineup is globally sorted by GuideNumber (ascending)."""
    status, body = _get_json(f"{base}/hdhr/lineup.json")
    if status != 200 or not isinstance(body, list):
        ctx.record("HDHR-04", False, f"status={status}")
        return
    guide_numbers = [entry.get("GuideNumber", "") for entry in body if isinstance(entry, dict)]

    def to_num(v: str) -> float:
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    nums = [to_num(g) for g in guide_numbers]
    is_sorted = nums == sorted(nums)
    ctx.record("HDHR-04", is_sorted, f"lineup has {len(nums)} entries, sorted={is_sorted}")


def _test_hdhr_05(ctx: _RecordCollector, base: str) -> None:
    """HDHR-05: Tune by vchannel: GET /tuner0/v{vchannel} delivers stream bytes."""
    status, lineup = _get_json(f"{base}/hdhr/lineup.json")
    if status != 200 or not isinstance(lineup, list) or not lineup:
        ctx.record("HDHR-05", False, "lineup unavailable for HDHR-05")
        return
    first = lineup[0] if isinstance(lineup[0], dict) else {}
    vchannel = first.get("GuideNumber", "")
    if not vchannel:
        ctx.record("HDHR-05", False, "no GuideNumber in lineup")
        return
    tune_url = f"{base}/tuner0/v{vchannel}"
    data = _read_stream_bytes(tune_url, MIN_STREAM_BYTES)
    ctx.record("HDHR-05", len(data) >= 1, f"read {len(data)} bytes from {tune_url}")


def _test_hdhr_08(ctx: _RecordCollector, base: str) -> None:
    """HDHR-08: /hdhr/device.xml returns valid UPnP XML."""
    req = urllib.request.Request(f"{base}/hdhr/device.xml", headers={"Accept": "application/xml"})
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            raw = resp.read()
            try:
                root = ElementTree.fromstring(raw)
                has_device = root.tag.endswith("device") or any(child.tag.endswith("device") for child in root)
                ctx.record("HDHR-08", True, f"parsed XML, root={root.tag} has_device_element={has_device}")
            except ElementTree.ParseError as exc:
                ctx.record("HDHR-08", False, f"XML parse error: {exc}")
    except Exception as exc:
        ctx.record("HDHR-08", False, str(exc))


def _test_hdhr_09(ctx: _RecordCollector, base: str) -> None:
    """HDHR-09: /hdhr/lineup.xml and /hdhr/lineup.m3u return equivalent channel data."""
    status_json, lineup_json = _get_json(f"{base}/hdhr/lineup.json")
    if status_json != 200 or not isinstance(lineup_json, list):
        ctx.record("HDHR-09", False, "lineup.json unavailable")
        return
    json_count = len(lineup_json)

    xml_count = 0
    try:
        req = urllib.request.Request(f"{base}/hdhr/lineup.xml")
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            root = ElementTree.fromstring(resp.read())
            xml_count = len(list(root))
    except Exception:
        pass

    m3u_count = 0
    try:
        req = urllib.request.Request(f"{base}/hdhr/lineup.m3u")
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            m3u_text = resp.read().decode("utf-8", errors="replace")
            m3u_count = m3u_text.count("#EXTINF")
    except Exception:
        pass

    xml_ok = xml_count == 0 or xml_count == json_count
    m3u_ok = m3u_count == 0 or m3u_count == json_count
    ctx.record("HDHR-09", xml_ok and m3u_ok, f"json={json_count} xml={xml_count} m3u={m3u_count}")


def _test_hdhr_10(ctx: _RecordCollector, base: str) -> None:
    """HDHR-10: /hdhr/auto/v{vchannel} works (any-tuner allocation)."""
    status, lineup = _get_json(f"{base}/hdhr/lineup.json")
    if status != 200 or not isinstance(lineup, list) or not lineup:
        ctx.record("HDHR-10", False, "lineup unavailable for HDHR-10")
        return
    first = lineup[0] if isinstance(lineup[0], dict) else {}
    vchannel = first.get("GuideNumber", "")
    if not vchannel:
        ctx.record("HDHR-10", False, "no GuideNumber in lineup")
        return
    tune_url = f"{base}/hdhr/auto/v{vchannel}"
    stream_status, bytes_read, stream_error = _read_stream_status(tune_url, num_bytes=MIN_STREAM_BYTES, timeout=15.0)
    ctx.record("HDHR-10", bytes_read >= 1, " ".join([f"status={stream_status}", f"bytes={bytes_read}", f"url={tune_url}", f"error={stream_error[:120]!r}"]))


def _test_hdhr_11(ctx: _RecordCollector, base: str) -> None:
    """HDHR-11: Legacy paths /discover.json and /lineup.json work identically to /hdhr/ prefixed."""
    status_hdhr, body_hdhr = _get_json(f"{base}/hdhr/discover.json")
    status_leg, body_leg = _get_json(f"{base}/discover.json")
    if status_hdhr != 200 or status_leg not in (200, 404):
        ctx.record("HDHR-11", False, f"hdhr={status_hdhr} legacy={status_leg}")
        return
    if status_leg == 404:
        ctx.record("HDHR-11", False, "legacy /discover.json returned 404 (not implemented)")
        return
    same = isinstance(body_hdhr, dict) and isinstance(body_leg, dict) and body_hdhr.get("DeviceID") == body_leg.get("DeviceID")
    ctx.record("HDHR-11", same, f"hdhr DeviceID={body_hdhr.get('DeviceID')} legacy DeviceID={body_leg.get('DeviceID')}")


def _test_hdhr_12(ctx: _RecordCollector, base: str) -> None:
    """HDHR-12: the real hdhomerun_config client can discover the emulated device."""
    binary = shutil.which("hdhomerun_config")
    if not binary:
        ctx.record("HDHR-12", None, "skip — requires hdhomerun_config binary")
        return

    status, body = _get_json(f"{base}/hdhr/discover.json")
    if status != 200 or not isinstance(body, dict):
        ctx.record("HDHR-12", False, f"/hdhr/discover.json returned {status}")
        return

    device_id = str(body.get("DeviceID") or "").strip()
    base_url = str(body.get("BaseURL") or "").strip()
    if not device_id:
        ctx.record("HDHR-12", False, "discover.json did not expose a DeviceID")
        return

    result = subprocess.run([binary, "discover"], capture_output=True, text=True, timeout=15.0, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    found_device = device_id in output

    ctx.record(
        "HDHR-12", result.returncode == 0 and found_device,
        " ".join([f"binary={binary}", f"returncode={result.returncode}", f"device_id={device_id}", f"base_url={base_url}", f"output={output.strip()[:120]!r}"]),
    )


def _test_hdhr_06(ctx: _RecordCollector, base: str) -> None:
    """HDHR-06: A second request on the same tuner retunes and replaces the prior subscriber."""
    status, lineup = _get_json(f"{base}/hdhr/lineup.json")
    if status != 200 or not isinstance(lineup, list) or len(lineup) < 2:
        ctx.record("HDHR-06", False, "Need at least two channels for tuner retune test")
        return

    guide_a = str(lineup[0].get("GuideNumber", ""))
    guide_b = str(lineup[1].get("GuideNumber", ""))
    if not guide_a or not guide_b:
        ctx.record("HDHR-06", False, "Missing GuideNumber values for tuner retune test")
        return

    first = HoldOpenStream(f"{base}/tuner0/v{guide_a}", hold_seconds=15.0)
    second = HoldOpenStream(f"{base}/tuner0/v{guide_b}", hold_seconds=15.0)
    try:
        first.start()
        first_status = first.wait_header()
        if first_status != 200:
            ctx.record("HDHR-06", False, f"first tune returned {first_status} error={first.error!r}")
            return

        second.start()
        second_status = second.wait_header()
        first_replaced = first.wait_finished(timeout_seconds=5.0)
        time.sleep(1.0)
        second_alive = not second.finished and second.error is None

        ctx.record(
            "HDHR-06", first_status == 200 and second_status == 200 and first_replaced and second_alive,
            " ".join([
                f"first_status={first_status}", f"second_status={second_status}", f"first_finished={first.finished}",
                f"first_reason={first.finish_reason!r}", f"second_finished={second.finished}",
                f"second_bytes={second.bytes_read}", f"second_error={second.error!r}",
            ]),
        )
    finally:
        first.stop()
        second.stop()


def _test_hdhr_07(ctx: _RecordCollector, client: M3UndleClient, base: str) -> None:
    """HDHR-07: Once two tuner slots are occupied, an auto-tune request is rejected until one frees."""
    settings_before = client.get_hdhr_settings()
    if not settings_before:
        ctx.record("HDHR-07", False, "GET /api/v1/settings/hdhr failed")
        return

    def restore_settings() -> None:
        client.update_hdhr_settings(
            enabled=bool(settings_before.get("enabled")), tuner_count_override=settings_before.get("tunerCountOverride"),
            advertised_base_url=settings_before.get("advertisedBaseUrl"), discovery_enabled=bool(settings_before.get("discoveryEnabled")),
            ssdp_enabled=bool(settings_before.get("ssdpEnabled")), silicon_dust_discovery_enabled=bool(settings_before.get("siliconDustDiscoveryEnabled")),
        )

    update_status, update_body = client.update_hdhr_settings(
        enabled=bool(settings_before.get("enabled")), tuner_count_override=2,
        advertised_base_url=settings_before.get("advertisedBaseUrl"), discovery_enabled=bool(settings_before.get("discoveryEnabled")),
        ssdp_enabled=bool(settings_before.get("ssdpEnabled")), silicon_dust_discovery_enabled=bool(settings_before.get("siliconDustDiscoveryEnabled")),
    )
    if update_status != 200 or not isinstance(update_body, dict):
        ctx.record("HDHR-07", False, f"PUT /api/v1/settings/hdhr returned {update_status}")
        return

    time.sleep(6.0)

    settings_active = client.get_hdhr_settings()
    status, lineup = _get_json(f"{base}/hdhr/lineup.json")
    if status != 200 or not isinstance(lineup, list) or len(lineup) < 3:
        restore_settings()
        ctx.record("HDHR-07", False, "Need at least three channels for tuner capacity test")
        return

    guides = [str(entry.get("GuideNumber", "")) for entry in lineup[:3]]
    if not all(guides):
        restore_settings()
        ctx.record("HDHR-07", False, "Missing GuideNumber values for tuner capacity test")
        return

    hold_a = HoldOpenStream(f"{base}/tuner0/v{guides[0]}", hold_seconds=15.0)
    hold_b = HoldOpenStream(f"{base}/tuner1/v{guides[1]}", hold_seconds=15.0)

    def wait_for_active_sessions_at_most(max_count: int, timeout_seconds: float = 15.0) -> list[dict]:
        deadline = time.monotonic() + timeout_seconds
        last_sessions: list[dict] = []
        while time.monotonic() < deadline:
            last_sessions = client.get_active_sessions()
            if len(last_sessions) <= max_count:
                return last_sessions
            time.sleep(0.5)
        return last_sessions

    try:
        hold_a.start()
        status_a = hold_a.wait_header()
        hold_b.start()
        status_b = hold_b.wait_header()
        sessions_during = client.wait_for_active_session_count(2, timeout_seconds=5.0)

        denied_status, _denied_bytes, denied_error = _read_stream_status(f"{base}/hdhr/auto/v{guides[2]}")

        hold_a.stop()
        sessions_after_stop = wait_for_active_sessions_at_most(1, timeout_seconds=15.0)
        retry_status, retry_bytes, retry_error = _read_stream_status(f"{base}/hdhr/auto/v{guides[2]}")

        ctx.record(
            "HDHR-07",
            status_a == 200 and status_b == 200 and len(sessions_during) >= 2 and denied_status == 503
            and retry_status == 200 and retry_bytes >= 1 and settings_active.get("effectiveTunerCount") == 2,
            " ".join([
                f"update_status={update_status}", f"effective_tuners={settings_active.get('effectiveTunerCount')}",
                f"restart_required={settings_active.get('restartRequired')}", f"first_status={status_a}", f"second_status={status_b}",
                f"active_sessions={len(sessions_during)}", f"active_after_stop={len(sessions_after_stop)}",
                f"first_finished={hold_a.finished}", f"second_finished={hold_b.finished}", f"denied_status={denied_status}",
                f"retry_status={retry_status}", f"retry_bytes={retry_bytes}", f"denied_error={denied_error[:60]!r}", f"retry_error={retry_error[:60]!r}",
            ]),
        )
    finally:
        hold_a.stop()
        hold_b.stop()
        client.reset_debug_state()
        time.sleep(1.0)
        restore_settings()


def _test_hdhr_ts_01(ctx: _RecordCollector, base: str) -> None:
    """HDHR-TS-01: HDHR tune routes deliver MPEG-TS bytes, not an HLS manifest or text fixture."""
    status, lineup = _get_json(f"{base}/hdhr/lineup.json")
    if status != 200 or not isinstance(lineup, list) or not lineup:
        ctx.record("HDHR-TS-01", False, "lineup.json unavailable for HDHR-TS-01")
        return
    first = lineup[0] if isinstance(lineup[0], dict) else {}
    vchannel = first.get("GuideNumber", "")
    if not vchannel:
        ctx.record("HDHR-TS-01", False, "no GuideNumber in lineup for HDHR-TS-01")
        return

    ts_min_bytes = 600
    routes = [(f"{base}/tuner0/v{vchannel}", f"/tuner0/v{vchannel}"), (f"{base}/hdhr/auto/v{vchannel}", f"/hdhr/auto/v{vchannel}")]

    parts: list[str] = []
    all_ok = True
    for url, label in routes:
        data = _read_stream_bytes(url, ts_min_bytes)
        bytes_read = len(data)
        is_hls_manifest = data[:8].startswith(b"#EXTM3U")
        has_bytes = bytes_read >= 1

        framing_ok = False
        if bytes_read >= 377:
            framing_ok = data[0:1] == b"\x47" and data[188:189] == b"\x47" and data[376:377] == b"\x47"
            ts_framing = f"ts_framing={'pass' if framing_ok else 'fail'}"
        else:
            ts_framing = f"ts_framing=skipped(only {bytes_read}B)"

        route_ok = has_bytes and not is_hls_manifest and framing_ok
        if not route_ok:
            all_ok = False
        parts.append(f"{label}:bytes={bytes_read},hls_manifest={is_hls_manifest},{ts_framing}")

    ctx.record("HDHR-TS-01", all_ok, " | ".join(parts))


def _test_hdhr_override_01(ctx: _RecordCollector, client: M3UndleClient, base: str) -> None:
    """HDHR-OVERRIDE-01: Guide-number override reaches published HDHR output after rebuild."""
    provider_channel_id, display_name = _get_first_channel(base)
    if not provider_channel_id:
        ctx.record("HDHR-OVERRIDE-01", False, "No channels available for override test")
        return

    override_guide = "9999"

    if not _patch_channel_number(base, provider_channel_id, override_guide):
        ctx.record("HDHR-OVERRIDE-01", False, f"PATCH channel number override failed for {provider_channel_id!r}")
        return

    try:
        client.build_snapshot()
    except RuntimeError as exc:
        ctx.record("HDHR-OVERRIDE-01", False, f"build_snapshot failed: {exc}")
        _patch_channel_number(base, provider_channel_id, None)
        return

    if not client.poll_build_completion(client.profile_id or "", timeout_seconds=90.0):
        ctx.record("HDHR-OVERRIDE-01", False, "Build did not settle after guide-number override")
        _patch_channel_number(base, provider_channel_id, None)
        return

    status_la, lineup_after = _get_json(f"{base}/hdhr/lineup.json")
    guide_numbers_after = {str(e.get("GuideNumber", "")) for e in (lineup_after if isinstance(lineup_after, list) else []) if isinstance(e, dict)}
    override_present = override_guide in guide_numbers_after

    _patch_channel_number(base, provider_channel_id, None)
    try:
        client.build_snapshot()
        client.poll_build_completion(client.profile_id or "", timeout_seconds=60.0)
    except Exception:
        pass

    ctx.record(
        "HDHR-OVERRIDE-01", override_present,
        " ".join([
            f"channel={display_name!r}", f"providerChannelId={provider_channel_id!r}", f"override_guide={override_guide!r}",
            f"guides_after={sorted(guide_numbers_after)!r}", f"override_present={override_present}",
        ]),
    )


def _test_hdhr_rst_01(ctx: _RecordCollector, client: M3UndleClient, base: str) -> None:
    """HDHR-RST-01: /hdhr/lineup.json is rebuilt from the active snapshot after restart."""
    status_a, lineup_a = _get_json(f"{base}/hdhr/lineup.json")
    if status_a != 200 or not isinstance(lineup_a, list) or not lineup_a:
        ctx.record("HDHR-RST-01", False, f"lineup.json unavailable before override: status={status_a}")
        return

    provider_channel_id, display_name = _get_first_channel(base)
    if not provider_channel_id:
        ctx.record("HDHR-RST-01", False, "No channels available to use as restart marker")
        return

    marker_guide = "9876"

    if not _patch_channel_number(base, provider_channel_id, marker_guide):
        ctx.record("HDHR-RST-01", False, f"PATCH channel override failed for {provider_channel_id!r}")
        return

    try:
        client.build_snapshot()
    except RuntimeError as exc:
        ctx.record("HDHR-RST-01", False, f"build_snapshot failed: {exc}")
        _patch_channel_number(base, provider_channel_id, None)
        return

    if not client.poll_build_completion(client.profile_id or "", timeout_seconds=90.0):
        ctx.record("HDHR-RST-01", False, "Build did not settle before restart")
        _patch_channel_number(base, provider_channel_id, None)
        return

    status_b, lineup_b_pre = _get_json(f"{base}/hdhr/lineup.json")
    guides_b_pre = {str(e.get("GuideNumber", "")) for e in (lineup_b_pre if isinstance(lineup_b_pre, list) else []) if isinstance(e, dict)}
    marker_before = marker_guide in guides_b_pre

    artifacts_dir = lab_common.runtime_results_dir()
    try:
        (artifacts_dir / "hdhr-rst-01-lineup-a.json").write_text(json.dumps(lineup_a, indent=2), encoding="utf-8")
        (artifacts_dir / "hdhr-rst-01-lineup-b-before-restart.json").write_text(json.dumps(lineup_b_pre, indent=2), encoding="utf-8")
    except Exception:
        pass

    if not marker_before:
        ctx.record("HDHR-RST-01", False, f"Marker guide {marker_guide!r} not visible before restart — cannot validate post-restart freshness")
        _patch_channel_number(base, provider_channel_id, None)
        return

    if not _restart_container_and_wait(base, timeout=90.0):
        ctx.record("HDHR-RST-01", False, "Container did not come live after restart")
        return

    if not client.poll_ready(timeout_seconds=60.0):
        ctx.record("HDHR-RST-01", False, "Container live but /health/ready did not return ready after restart")
        return

    status_post, lineup_post = _get_json(f"{base}/hdhr/lineup.json")
    guides_post = {str(e.get("GuideNumber", "")) for e in (lineup_post if isinstance(lineup_post, list) else []) if isinstance(e, dict)}
    marker_after = marker_guide in guides_post

    try:
        (artifacts_dir / "hdhr-rst-01-lineup-b-after-restart.json").write_text(json.dumps(lineup_post, indent=2), encoding="utf-8")
    except Exception:
        pass

    _patch_channel_number(base, provider_channel_id, None)
    try:
        client.build_snapshot()
        client.poll_build_completion(client.profile_id or "", timeout_seconds=60.0)
    except Exception:
        pass

    ctx.record(
        "HDHR-RST-01", marker_after,
        " ".join([
            f"marker_guide={marker_guide!r}", f"marker_before_restart={marker_before}", f"marker_after_restart={marker_after}",
            f"guides_pre={sorted(guides_b_pre)!r}", f"guides_post={sorted(guides_post)!r}", f"post_restart_status={status_post}",
        ]),
    )


def _test_hdhr_relay_01(ctx: _RecordCollector, client: M3UndleClient, base: str) -> None:
    """HDHR-RELAY-01: unstable channel selects FFmpeg clean relay -- not silent direct-HTTP fallback."""
    expected_relay = "FfmpegCleanRemux"
    direct_relay = "Direct"

    provider_id = client.provider_id
    if not provider_id:
        ctx.record("HDHR-RELAY-01", False, "provider_id not set — must run after setup")
        return

    status, lineup = _get_json(f"{base}/hdhr/lineup.json")
    if status != 200 or not isinstance(lineup, list) or not lineup:
        ctx.record("HDHR-RELAY-01", False, f"lineup unavailable: status={status}")
        return
    first_entry = lineup[0] if isinstance(lineup[0], dict) else {}
    vchannel = first_entry.get("GuideNumber", "")
    if not vchannel:
        ctx.record("HDHR-RELAY-01", False, "no GuideNumber in lineup")
        return

    provider_channel_id, display_name = _get_first_channel(base)
    if not provider_channel_id:
        ctx.record("HDHR-RELAY-01", False, "could not resolve providerChannelId for health seeding")
        return

    enc_provider = urllib.parse.quote(str(provider_id), safe="")
    enc_channel = urllib.parse.quote(provider_channel_id, safe="")
    health_events_path = f"/debug/stream-health/events/{enc_provider}/{enc_channel}"
    health_path = f"/debug/stream-health/{enc_provider}/{enc_channel}"

    def cleanup() -> None:
        client.delete(health_events_path)

    def wait_for_no_active_sessions(timeout_seconds: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not client.get_active_sessions():
                return True
            time.sleep(0.5)
        return not client.get_active_sessions()

    def relay_value(data: dict) -> str:
        for key in ("relayMode", "RelayMode", "upstreamRelayMode", "UpstreamRelayMode"):
            val = data.get(key)
            if val:
                return str(val)
        for key, val in data.items():
            if "relay" in key.lower() and val:
                return str(val)
        return ""

    def session_matches_channel(data: dict) -> bool:
        session_channel = str(data.get("providerChannelId") or data.get("ProviderChannelId") or "")
        session_name = str(data.get("displayName") or data.get("DisplayName") or "")
        return session_channel == provider_channel_id or session_name == display_name

    now = datetime.now(UTC)

    def fmt(dt: datetime) -> str:
        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")

    seed_status, _ = client.post("/debug/stream-health/events/seed", {
        "providerId": str(provider_id), "providerChannelId": provider_channel_id, "displayName": display_name,
        "events": [
            {"eventKind": "RecoveryOutputResumed", "eventUtc": fmt(now), "safeStartKind": "H264Idr", "relayMode": direct_relay, "sessionId": "hdhr-relay-01-seed-a"},
            {"eventKind": "MpegTsSyncLost", "eventUtc": fmt(now + timedelta(seconds=10)), "tsSyncLoss": True, "relayMode": direct_relay, "sessionId": "hdhr-relay-01-seed-b"},
            {"eventKind": "MpegTsSyncLost", "eventUtc": fmt(now + timedelta(seconds=20)), "tsSyncLoss": True, "relayMode": direct_relay, "sessionId": "hdhr-relay-01-seed-c"},
        ],
    })
    if seed_status != 200:
        ctx.record("HDHR-RELAY-01", False, f"health seed failed: status={seed_status}")
        return

    health_status, health = client.get(health_path)
    if health_status != 200 or not isinstance(health, dict):
        cleanup()
        ctx.record("HDHR-RELAY-01", False, f"health query failed: status={health_status}")
        return

    auto_decision = health.get("autoRelayDecision") or {}
    pre_relay = str(auto_decision.get("selectedRelayMode") or "")
    policy = health.get("recoveryPolicy") or {}
    profile_raw = policy.get("profile")
    profile_names = {0: "Stable", 1: "Cautious", 2: "Unstable"}
    profile_label = profile_names.get(profile_raw, f"Unknown({profile_raw})") if isinstance(profile_raw, int) else str(profile_raw)

    if pre_relay != expected_relay:
        cleanup()
        ctx.record("HDHR-RELAY-01", False, f"channel did not reach Unstable+FfmpegCleanRemux after seeding: profile={profile_label!r} pre_relay={pre_relay!r}")
        return

    client.reset_debug_state()
    if not wait_for_no_active_sessions(timeout_seconds=15.0):
        cleanup()
        ctx.record("HDHR-RELAY-01", False, "active sessions did not drain before relay assertion")
        return

    tune_url = f"{base}/tuner0/v{vchannel}"
    stream = HoldOpenStream(tune_url, hold_seconds=25.0)
    actual_relay = ""
    session_id = ""
    try:
        stream.start()
        hdr = stream.wait_header(timeout_seconds=15.0)
        if hdr != 200:
            cleanup()
            ctx.record("HDHR-RELAY-01", False, f"stream did not open (pre_relay={pre_relay!r}): status={hdr} error={stream.error!r}")
            return

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and stream.bytes_read < MIN_STREAM_BYTES:
            time.sleep(0.25)

        sessions = client.wait_for_active_session_count(1, timeout_seconds=10.0)
        if sessions:
            s = next((item for item in sessions if session_matches_channel(item)), sessions[0])
            session_id = str(s.get("sessionId") or s.get("SessionId") or "")
            actual_relay = relay_value(s)

        if session_id and not actual_relay:
            session_data = client.get_session_status(session_id)
            if isinstance(session_data, dict):
                actual_relay = relay_value(session_data)

        if not actual_relay:
            events = client.get_stream_events(sessionId=session_id) if session_id else client.get_stream_events()
            for evt in (events if isinstance(events, list) else []):
                if not isinstance(evt, dict):
                    continue
                evt_session = str(evt.get("sessionId") or evt.get("SessionId") or "")
                if session_id and evt_session and evt_session != session_id:
                    continue
                actual_relay = relay_value(evt)
                if actual_relay:
                    break
    finally:
        stream.stop()
        cleanup()

    bytes_ok = stream.bytes_read >= MIN_STREAM_BYTES
    relay_confirmed = actual_relay == expected_relay
    fallback_taken = actual_relay == direct_relay
    relay_undetected = not actual_relay
    passed = bytes_ok and relay_confirmed

    ctx.record(
        "HDHR-RELAY-01", passed,
        " ".join([
            f"profile={profile_label!r}", f"pre_relay={pre_relay!r}", f"actual_relay={actual_relay!r}", f"bytes={stream.bytes_read}",
            f"session_id={session_id!r}", f"fallback_taken={fallback_taken}", f"relay_undetected={relay_undetected}",
        ]),
    )


def _test_hdhr_unenforced_01(ctx: _RecordCollector, client: M3UndleClient, base: str, provider_name: str, playlist_url: str) -> None:
    """HDHR-UNENFORCED-01: With no stream limit enforced, two concurrent tuners both work."""
    settings_before = client.get_hdhr_settings()
    if not settings_before:
        ctx.record("HDHR-UNENFORCED-01", False, "GET /api/v1/settings/hdhr failed")
        return

    def restore_state() -> None:
        client.update_hdhr_settings(
            enabled=bool(settings_before.get("enabled")), tuner_count_override=settings_before.get("tunerCountOverride"),
            advertised_base_url=settings_before.get("advertisedBaseUrl"), discovery_enabled=bool(settings_before.get("discoveryEnabled")),
            ssdp_enabled=bool(settings_before.get("ssdpEnabled")), silicon_dust_discovery_enabled=bool(settings_before.get("siliconDustDiscoveryEnabled")),
        )
        client.upsert_provider(name=provider_name, playlist_url=playlist_url, max_concurrent_streams=4)

    update_status, _ = client.update_hdhr_settings(
        enabled=bool(settings_before.get("enabled")), tuner_count_override=None,
        advertised_base_url=settings_before.get("advertisedBaseUrl"), discovery_enabled=bool(settings_before.get("discoveryEnabled")),
        ssdp_enabled=bool(settings_before.get("ssdpEnabled")), silicon_dust_discovery_enabled=bool(settings_before.get("siliconDustDiscoveryEnabled")),
    )
    client.upsert_provider(name=provider_name, playlist_url=playlist_url, max_concurrent_streams=None)

    if update_status != 200:
        restore_state()
        ctx.record("HDHR-UNENFORCED-01", False, f"PUT /api/v1/settings/hdhr returned {update_status}")
        return

    time.sleep(6.0)

    settings_active = client.get_hdhr_settings()
    is_enforced = bool(settings_active.get("isStreamLimitEnforced"))
    effective_count = settings_active.get("effectiveTunerCount")

    status, lineup = _get_json(f"{base}/hdhr/lineup.json")
    if status != 200 or not isinstance(lineup, list) or len(lineup) < 2:
        restore_state()
        ctx.record("HDHR-UNENFORCED-01", False, "Need at least two channels for concurrent-tuner test")
        return

    guide_a = str(lineup[0].get("GuideNumber", ""))
    guide_b = str(lineup[1].get("GuideNumber", ""))
    if not guide_a or not guide_b:
        restore_state()
        ctx.record("HDHR-UNENFORCED-01", False, "Missing GuideNumber values for concurrent-tuner test")
        return

    hold_a = HoldOpenStream(f"{base}/tuner0/v{guide_a}", hold_seconds=10.0)
    hold_b = HoldOpenStream(f"{base}/tuner1/v{guide_b}", hold_seconds=10.0)
    try:
        hold_a.start()
        status_a = hold_a.wait_header()
        hold_b.start()
        status_b = hold_b.wait_header()
        sessions_during = client.wait_for_active_session_count(2, timeout_seconds=5.0)

        ctx.record(
            "HDHR-UNENFORCED-01",
            not is_enforced and isinstance(effective_count, int) and effective_count > 1 and status_a == 200 and status_b == 200 and len(sessions_during) >= 2,
            " ".join([
                f"is_enforced={is_enforced}", f"effective_tuners={effective_count}", f"first_status={status_a}",
                f"second_status={status_b}", f"active_sessions={len(sessions_during)}",
            ]),
        )
    finally:
        hold_a.stop()
        hold_b.stop()
        restore_state()


# ---------------------------------------------------------------------------
# Setup: run the whole original orchestration once, collecting each case's
# own outcome; cases below just replay it.
# ---------------------------------------------------------------------------

@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    base = base_url.rstrip("/")
    collector = _RecordCollector()
    sim: SimulatorInstance | None = None
    state: dict[str, object] = {"reason": "HDHR suite setup did not complete", "records": collector.records}

    try:
        setup_provider_name = f"provider-hdhr-sim-{int(time.time())}"
        bind, public_host = _simulator_address()

        sim = SimulatorInstance(fixture=FIXTURE, port=SIM_PORT, bind=bind, public_host=public_host, max_streams=8, suite="hdhr")
        sim.start()
        if not sim.wait_healthy():
            state["reason"] = "Provider simulator did not become healthy"
            return {"state": state}
        sim_url = sim.playlist_url

        client = M3UndleClient(base)
        if not client.setup(playlist_url=sim_url, provider_name=setup_provider_name, max_concurrent_streams=4):
            state["reason"] = client.last_setup_error or "Setup sequence failed"
            return {"state": state}

        stream_urls = client.get_stream_urls()
        expected_channels = len(stream_urls)

        _test_hdhr_01(collector, base)
        _test_hdhr_02(collector, base)
        _test_hdhr_03(collector, base, expected_channels)
        _test_hdhr_04(collector, base)
        _test_hdhr_05(collector, base)

        _test_hdhr_06(collector, base)
        _test_hdhr_07(collector, client, base)

        _test_hdhr_08(collector, base)
        _test_hdhr_09(collector, base)
        _test_hdhr_10(collector, base)
        _test_hdhr_11(collector, base)

        _test_hdhr_12(collector, base)

        _test_hdhr_ts_01(collector, base)
        _test_hdhr_override_01(collector, client, base)

        _test_hdhr_relay_01(collector, client, base)

        # UNENFORCED-01 mutates HDHR settings + the provider's stream limit (each change
        # triggers a refresh); run it after the steady-state tests so it doesn't disturb them.
        _test_hdhr_unenforced_01(collector, client, base, setup_provider_name, sim_url)

        # RST-01 restarts the container; run it last so it does not disrupt earlier tests.
        _test_hdhr_rst_01(collector, client, base)

        state["reason"] = None
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"HDHR suite setup failed: {exc}"
        return {"state": state}
    finally:
        if sim is not None:
            try:
                sim.stop()
            except Exception:
                pass


def _register_case(case_id: str) -> None:
    @SUITE.case(case_id)
    def _replay(ctx, state):
        records: dict[str, tuple[bool | None, str, Any]] = state["records"]  # type: ignore[assignment]
        entry = records.get(case_id)
        if entry is None:
            ctx.skip(case_id, str(state.get("reason") or "HDHR suite setup did not reach this case"))
            return
        passed, message, detail = entry
        ctx.record(case_id, passed, message, detail)


for _case_id in CASE_IDS:
    _register_case(_case_id)
