"""Port of the frozen Alpha 5 Xtream Codes API compatibility suite
(XTR-01..08, XTR-WEB-01, PROV-07..08).

M3Undle is set up with a standard M3U provider first; its Xtream output
endpoints (get_account_info, categories, streams, path-credential streaming,
get.php M3U, VOD/series JSON) are verified against that. A second pass swaps
in a web-player HLS-source provider to confirm Electron/browser UA handling
stays on the right transport. A third pass swaps to a native upstream Xtream
provider and re-verifies both the standard M3U and Xtream outputs against it.

Structural adaptation, not a behavior change: same as test_profiles.py --
the frozen script is one long imperative main() with a deep proceed-chain and
some paths that leave a case unrecorded entirely. @SUITE.setup runs the exact
same sequence (test functions ported unchanged, called against a
_RecordCollector standing in for the original RunContext) and stores each
one's outcome; each registered case replays its own precomputed result.

Also replaces the frozen suite's role-gated (srv1-only) temporary-
encryption-key restart with the same role-free _restart_with_env pattern
test_encryption_rotation.py/test_playback_contract.py already use -- se-lab
dropped the srv1/srv2 role concept entirely this migration, so "only runs
when current_role() == srv1" no longer describes any real deployment; the
retry now just always applies when the missing-key error appears.
"""

from __future__ import annotations

import base64
import platform
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent import common as lab_common
from agent.container import get_docker_gateway, wait_up
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.commands import CONTAINER_NAME, HOST_OVERRIDE
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("xtream", group="core", order=135)
SIM_PORT_M3U = 19016
SIM_PORT_XTR = 19017
SIM_PORT_WEB_HLS = 19018
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
FIXTURE_M3U = FIXTURES_DIR / "provider-a.json"
FIXTURE_XTR = FIXTURES_DIR / "provider-xtream.json"
FIXTURE_WEB_HLS = FIXTURES_DIR / "provider-web-hls.json"

XTREAM_USER = "testuser"
XTREAM_PASS = "testpass"
UPSTREAM_XTREAM_USER = "xtreamuser"
UPSTREAM_XTREAM_PASS = "xtreampass"
EXPECTED_XTREAM_CHANNEL_NAMES = ("Xtream Sports 1", "Xtream Sports 2", "Xtream News 1")

MIN_STREAM_BYTES = 1024
TS_FRAMING_MIN_BYTES = 600
ENCRYPTION_KEY_ENV = "M3UNDLE_ENCRYPTION_KEY"

IPTVNATOR_ELECTRON_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) "
    "IPTVnator/0.16.0 Chrome/120.0.6099.291 Electron/28.1.0 Safari/537.36"
)
CHROME_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

CASE_IDS = ["XTR-01", "XTR-02", "XTR-03", "XTR-04", "XTR-05", "XTR-06", "XTR-07", "XTR-08", "XTR-WEB-01", "PROV-07", "PROV-08"]


class _RecordCollector:
    def __init__(self) -> None:
        self.records: dict[str, tuple[bool | None, str, Any]] = {}

    def record(self, name: str, passed: bool | None, message: str, detail: Any = None) -> None:
        self.records[name] = (passed, message, detail)


def _simulator_address(port: int) -> tuple[str, str | None]:
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{port}"
    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{port}"
    return "127.0.0.1", None


def _generate_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _restart_with_env(base_url: str, values: dict[str, str]) -> tuple[bool, str]:
    lab_common.set_runtime_env_values(values)
    try:
        lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
    except Exception as exc:
        return False, f"compose up failed: {exc}"
    if not wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health")):
        return False, "stack did not become healthy after restart"
    return True, "restarted and healthy"


# ---------------------------------------------------------------------------
# Helpers (ported unchanged)
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_301(self, req, fp, code, msg, headers):  # type: ignore[override]
        return fp
    http_error_302 = http_error_303 = http_error_307 = http_error_308 = http_error_301


def _xtream_get(base: str, action: str, user: str = XTREAM_USER, pwd: str = XTREAM_PASS) -> tuple[int, object]:
    import json
    url = f"{base}/player_api.php?username={user}&password={pwd}&action={action}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10.0) as resp:
            try:
                return resp.status, json.loads(resp.read())
            except Exception:
                return resp.status, {}
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {}
    except Exception as exc:
        return 0, {"error": str(exc)}


def _xtream_get_m3u(base: str, user: str = XTREAM_USER, pwd: str = XTREAM_PASS) -> tuple[int, str]:
    url = f"{base}/get.php?username={user}&password={pwd}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10.0) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:
        return 0, str(exc)


def _get_standard_m3u(client: M3UndleClient, user: str = XTREAM_USER, pwd: str = XTREAM_PASS) -> tuple[int, str]:
    status, body = client.get(f"/m3u/m3undle.m3u?username={user}&password={pwd}")
    return status, body if isinstance(body, str) else ""


def _has_expected_channel_names(body: str, expected_names: tuple[str, ...]) -> bool:
    return all(name in body for name in expected_names)


def _read_stream_bytes(url: str, num_bytes: int = MIN_STREAM_BYTES, timeout: float = 15.0) -> bytes:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            return resp.read(num_bytes)
    except Exception:
        return b""


def _ts_framing_ok(data: bytes) -> bool:
    return len(data) >= 377 and data[0:1] == b"\x47" and data[188:189] == b"\x47" and data[376:377] == b"\x47"


def _probe_xtream_stream(url: str, user_agent: str, read_bytes: int = TS_FRAMING_MIN_BYTES) -> dict:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    try:
        with opener.open(req, timeout=20.0) as resp:
            status = getattr(resp, "status", resp.getcode())
            data = resp.read(read_bytes) if status == 200 else b""
            return {
                "status": status, "content_type": resp.headers.get("Content-Type", ""),
                "location": resp.headers.get("Location", ""), "bytes": len(data),
                "hls_manifest": data[:8].startswith(b"#EXTM3U"), "ts_framing": _ts_framing_ok(data),
            }
    except urllib.error.HTTPError as exc:
        data = exc.read(read_bytes) if exc.code == 200 else b""
        return {
            "status": exc.code, "content_type": exc.headers.get("Content-Type", ""),
            "location": exc.headers.get("Location", ""), "bytes": len(data),
            "hls_manifest": data[:8].startswith(b"#EXTM3U"), "ts_framing": _ts_framing_ok(data),
        }
    except Exception as exc:
        return {"status": 0, "content_type": "", "location": "", "bytes": 0, "hls_manifest": False, "ts_framing": False, "error": str(exc)}


def _enable_auth(client: M3UndleClient, username: str = XTREAM_USER, password: str = XTREAM_PASS) -> bool:
    status, _ = client._request("PUT", "/api/v1/settings/endpoint-security", body={"enabled": True, "username": username, "password": password})
    return status in (200, 204)


def _disable_auth(client: M3UndleClient) -> bool:
    status, _ = client._request("PUT", "/api/v1/settings/endpoint-security", body={"enabled": False})
    return status in (200, 204)


# ---------------------------------------------------------------------------
# Test functions (ported unchanged -- collector duck-types RunContext.record)
# ---------------------------------------------------------------------------

def _test_xtr_01(ctx: _RecordCollector, base: str) -> None:
    """XTR-01: get_account_info echoes back the submitted URL credential."""
    status, body = _xtream_get(base, "get_account_info")
    if status != 200 or not isinstance(body, dict):
        ctx.record("XTR-01", False, f"status={status} body={str(body)[:120]}")
        return
    user_info = body.get("user_info", {})
    if not isinstance(user_info, dict):
        ctx.record("XTR-01", False, f"user_info missing or not dict: {str(body)[:120]}")
        return
    password_val = user_info.get("password", "NOT_PRESENT")
    correctly_echoed = password_val == XTREAM_PASS
    has_username = bool(user_info.get("username"))
    ctx.record(
        "XTR-01", has_username and correctly_echoed,
        f"username={user_info.get('username')!r} password={password_val!r} expected={XTREAM_PASS!r} correctly_echoed={correctly_echoed}",
    )


def _test_xtr_02(ctx: _RecordCollector, base: str) -> None:
    """XTR-02: get_live_categories returns M3Undle groups as Xtream categories."""
    status, body = _xtream_get(base, "get_live_categories")
    if status != 200 or not isinstance(body, list):
        ctx.record("XTR-02", False, f"status={status} type={type(body).__name__}")
        return
    has_category_fields = all(isinstance(c, dict) and "category_id" in c and "category_name" in c for c in body) if body else False
    ctx.record("XTR-02", len(body) > 0 and has_category_fields, f"categories={len(body)} has_fields={has_category_fields} first={str(body[:1])[:80]}")


def _test_xtr_03(ctx: _RecordCollector, base: str) -> list[dict]:
    """XTR-03: get_live_streams returns stable stream_ids (consistent across two calls)."""
    status1, body1 = _xtream_get(base, "get_live_streams")
    status2, body2 = _xtream_get(base, "get_live_streams")
    if status1 != 200 or not isinstance(body1, list):
        ctx.record("XTR-03", False, f"first call status={status1}")
        return []
    if status2 != 200 or not isinstance(body2, list):
        ctx.record("XTR-03", False, f"second call status={status2}")
        return []
    ids1 = {s.get("stream_id") for s in body1 if isinstance(s, dict)}
    ids2 = {s.get("stream_id") for s in body2 if isinstance(s, dict)}
    stable = ids1 == ids2 and bool(ids1)
    ctx.record("XTR-03", stable, f"streams={len(body1)} ids_consistent={stable} sample_id={next(iter(ids1), None)}")
    return body1


def _test_xtr_04(ctx: _RecordCollector, base: str, streams: list[dict]) -> None:
    """XTR-04: Path-credential streaming delivers stream bytes."""
    if not streams:
        ctx.record("XTR-04", False, "no streams from XTR-03 to test")
        return
    stream = next((s for s in streams if isinstance(s, dict) and s.get("stream_id")), None)
    if not stream:
        ctx.record("XTR-04", False, "no valid stream entry found")
        return
    stream_id = stream["stream_id"]
    tune_url = f"{base}/live/{XTREAM_USER}/{XTREAM_PASS}/{stream_id}.ts"
    data = _read_stream_bytes(tune_url, MIN_STREAM_BYTES)
    ctx.record("XTR-04", len(data) >= 1, f"read {len(data)} bytes from /live/{XTREAM_USER}/{XTREAM_PASS}/{stream_id}.ts")


def _test_xtr_05(ctx: _RecordCollector, base: str, streams: list[dict]) -> None:
    """XTR-05: Path-credential with wrong password returns 403."""
    if not streams:
        ctx.record("XTR-05", False, "no streams to test")
        return
    stream = next((s for s in streams if isinstance(s, dict) and s.get("stream_id")), None)
    if not stream:
        ctx.record("XTR-05", False, "no valid stream entry found")
        return
    stream_id = stream["stream_id"]
    url = f"{base}/live/{XTREAM_USER}/wrongpass/{stream_id}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10.0) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception:
        status = 0
    ctx.record("XTR-05", status in (401, 403), f"expected 401 or 403, got {status}")


def _test_xtr_06(ctx: _RecordCollector, base: str) -> None:
    """XTR-06: GET /get.php returns valid M3U with Xtream-style entries."""
    status, body = _xtream_get_m3u(base)
    is_m3u = "#EXTM3U" in body and "#EXTINF" in body
    ctx.record("XTR-06", status == 200 and is_m3u, f"status={status} has_header={'#EXTM3U' in body} has_entries={'#EXTINF' in body}")


def _test_xtr_07(ctx: _RecordCollector, base: str) -> None:
    """XTR-07: VOD and series category/stream endpoints return valid JSON."""
    results = {}
    for action in ("get_vod_categories", "get_vod_streams", "get_series_categories", "get_series"):
        status, body = _xtream_get(base, action)
        results[action] = (status, isinstance(body, list))
    all_ok = all(status == 200 and is_list for status, is_list in results.values())
    ctx.record("XTR-07", all_ok, " ".join(f"{a}={s}" for a, (s, _) in results.items()))


def _test_xtr_08(ctx: _RecordCollector, base: str) -> None:
    """XTR-08: get_series_info for any series_id returns response."""
    import json
    url = f"{base}/player_api.php?username={XTREAM_USER}&password={XTREAM_PASS}&action=get_series_info&series_id=1"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10.0) as resp:
            body = json.loads(resp.read())
            ctx.record("XTR-08", isinstance(body, dict), f"status=200 body_keys={list(body.keys())[:5]}")
    except urllib.error.HTTPError as exc:
        ctx.record("XTR-08", False, f"HTTPError {exc.code}")
    except Exception as exc:
        ctx.record("XTR-08", False, str(exc))


def _test_xtr_web_01(ctx: _RecordCollector, base: str, streams: list[dict]) -> None:
    """XTR-WEB-01: Electron web TS players stay on MPEG-TS for HLS-sourced Xtream streams."""
    stream = next((s for s in streams if isinstance(s, dict) and s.get("stream_id")), None)
    if not stream:
        ctx.record("XTR-WEB-01", False, "no live stream available for web-player HLS regression")
        return

    stream_id = stream["stream_id"]
    tune_url = f"{base}/live/{XTREAM_USER}/{XTREAM_PASS}/{stream_id}.ts"
    electron = _probe_xtream_stream(tune_url, IPTVNATOR_ELECTRON_UA)
    browser = _probe_xtream_stream(tune_url, CHROME_BROWSER_UA, read_bytes=0)
    explicit_hls = _probe_xtream_stream(f"{tune_url}?format=hls", IPTVNATOR_ELECTRON_UA, read_bytes=0)

    electron_content_type = str(electron.get("content_type", "")).lower()
    electron_ok = (
        electron.get("status") == 200 and not electron.get("location")
        and "video/mp2t" in electron_content_type and electron.get("bytes", 0) >= TS_FRAMING_MIN_BYTES
        and not electron.get("hls_manifest") and electron.get("ts_framing") is True
    )
    browser_ok = (
        browser.get("status") in (301, 302, 303, 307, 308)
        and "/hls/generated/" in str(browser.get("location", "")) and str(browser.get("location", "")).endswith("/index.m3u8")
    )
    explicit_hls_ok = explicit_hls.get("status") in (301, 302, 303, 307, 308) and "/hls/generated/" in str(explicit_hls.get("location", ""))

    ctx.record(
        "XTR-WEB-01", electron_ok and browser_ok and explicit_hls_ok,
        " | ".join([
            f"electron=status={electron.get('status')} ct={electron.get('content_type')!r} bytes={electron.get('bytes')} "
            f"hls_manifest={electron.get('hls_manifest')} ts_framing={electron.get('ts_framing')} location={electron.get('location')!r}",
            f"browser=status={browser.get('status')} location={browser.get('location')!r}",
            f"format_hls=status={explicit_hls.get('status')} location={explicit_hls.get('location')!r}",
        ]),
        detail={"electron": electron, "browser": browser, "format_hls": explicit_hls},
    )


def _test_prov_07(ctx: _RecordCollector, client: M3UndleClient, *, expected_base_url: str, expected_username: str) -> None:
    """PROV-07: Create a native Xtream provider via POST /api/v1/providers."""
    if not client.provider_id:
        ctx.record("PROV-07", False, "provider_id missing after Xtream setup")
        return
    status, body = client.get(f"/api/v1/providers/{client.provider_id}")
    if status != 200 or not isinstance(body, dict):
        ctx.record("PROV-07", False, f"GET provider returned {status}: {str(body)[:120]}")
        return
    is_xtream = body.get("isXtreamProvider") is True
    base_matches = body.get("xtreamBaseUrl") == expected_base_url.rstrip("/")
    user_matches = body.get("xtreamUsername") == expected_username
    playlist_blank = body.get("playlistUrl") in ("", None)
    ctx.record(
        "PROV-07", is_xtream and base_matches and user_matches and playlist_blank,
        " ".join([
            f"isXtreamProvider={body.get('isXtreamProvider')!r}", f"xtreamBaseUrl={body.get('xtreamBaseUrl')!r}",
            f"xtreamUsername={body.get('xtreamUsername')!r}", f"playlistUrl={body.get('playlistUrl')!r}",
        ]),
    )


def _test_prov_08(ctx: _RecordCollector, client: M3UndleClient, base: str) -> None:
    """PROV-08: Xtream-backed snapshots publish valid M3U and Xtream outputs."""
    status_m3u, body_m3u = _get_standard_m3u(client)
    status_xtream_m3u, body_xtream_m3u = _xtream_get_m3u(base)
    status_streams, body_streams = _xtream_get(base, "get_live_streams")

    standard_ok = status_m3u == 200 and "#EXTM3U" in body_m3u and _has_expected_channel_names(body_m3u, EXPECTED_XTREAM_CHANNEL_NAMES)
    xtream_ok = status_xtream_m3u == 200 and "#EXTM3U" in body_xtream_m3u and _has_expected_channel_names(body_xtream_m3u, EXPECTED_XTREAM_CHANNEL_NAMES)
    streams_ok = status_streams == 200 and isinstance(body_streams, list) and len(body_streams) >= len(EXPECTED_XTREAM_CHANNEL_NAMES)
    ctx.record(
        "PROV-08", standard_ok and xtream_ok and streams_ok,
        " ".join([
            f"/m3u_status={status_m3u}", f"/get.php_status={status_xtream_m3u}", f"live_streams_status={status_streams}",
            f"standard_names={_has_expected_channel_names(body_m3u, EXPECTED_XTREAM_CHANNEL_NAMES)}",
            f"xtream_names={_has_expected_channel_names(body_xtream_m3u, EXPECTED_XTREAM_CHANNEL_NAMES)}",
            f"stream_count={(len(body_streams) if isinstance(body_streams, list) else 0)}",
        ]),
    )


# ---------------------------------------------------------------------------
# Setup: run the whole original orchestration once, collecting each case's
# own outcome; cases below just replay it.
# ---------------------------------------------------------------------------

@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    base = base_url.rstrip("/")
    collector = _RecordCollector()
    sim_m3u: SimulatorInstance | None = None
    sim_xtr: SimulatorInstance | None = None
    sim_web_hls: SimulatorInstance | None = None
    client: M3UndleClient | None = None
    state: dict[str, object] = {"reason": "Xtream suite setup did not complete", "records": collector.records}
    restarted_encryption_key = False
    original_encryption_key: str | None = None

    try:
        setup_provider_name = f"provider-xtr-m3u-sim-{int(time.time())}"
        bind_m3u, public_host_m3u = _simulator_address(SIM_PORT_M3U)
        bind_xtr, public_host_xtr = _simulator_address(SIM_PORT_XTR)
        bind_web_hls, public_host_web_hls = _simulator_address(SIM_PORT_WEB_HLS)

        sim_m3u = SimulatorInstance(fixture=FIXTURE_M3U, port=SIM_PORT_M3U, bind=bind_m3u, public_host=public_host_m3u, suite="xtream-m3u")
        sim_m3u.start()
        if not sim_m3u.wait_healthy():
            state["reason"] = "Provider simulator did not become healthy"
            return {"state": state}
        sim_url = sim_m3u.playlist_url

        client = M3UndleClient(base)
        if not client.setup(playlist_url=sim_url, provider_name=setup_provider_name):
            state["reason"] = client.last_setup_error or "Setup sequence failed"
            return {"state": state}

        if not _enable_auth(client):
            state["reason"] = "Could not enable endpoint security for Xtream tests"
            return {"state": state}

        _test_xtr_01(collector, base)
        _test_xtr_02(collector, base)
        streams = _test_xtr_03(collector, base)
        _test_xtr_04(collector, base, streams)
        _test_xtr_05(collector, base, streams)
        _test_xtr_06(collector, base)
        _test_xtr_07(collector, base)
        _test_xtr_08(collector, base)

        _disable_auth(client)
        sim_web_hls = SimulatorInstance(fixture=FIXTURE_WEB_HLS, port=SIM_PORT_WEB_HLS, bind=bind_web_hls, public_host=public_host_web_hls, suite="xtream-web-hls")
        sim_web_hls.start()
        if not sim_web_hls.wait_healthy():
            state["reason"] = "Web-player HLS simulator did not become healthy"
            return {"state": state}

        web_provider_name = f"provider-web-hls-sim-{int(time.time())}"
        client = M3UndleClient(base)
        if not client.setup(playlist_url=sim_web_hls.playlist_url, provider_name=web_provider_name):
            state["reason"] = client.last_setup_error or "Web-player HLS setup failed"
            return {"state": state}
        if not _enable_auth(client):
            state["reason"] = "Could not enable endpoint security for web-player HLS regression"
            return {"state": state}

        status, body = _xtream_get(base, "get_live_streams")
        web_streams = body if status == 200 and isinstance(body, list) else []
        if status != 200:
            state["reason"] = f"get_live_streams returned status={status}"
            return {"state": state}
        _test_xtr_web_01(collector, base, web_streams)

        _disable_auth(client)
        sim_xtr = SimulatorInstance(fixture=FIXTURE_XTR, port=SIM_PORT_XTR, bind=bind_xtr, public_host=public_host_xtr, suite="xtream-provider")
        sim_xtr.start()
        if not sim_xtr.wait_healthy():
            state["reason"] = "Xtream simulator did not become healthy"
            return {"state": state}

        xtream_provider_name = f"provider-native-xtream-sim-{int(time.time())}"
        ok = client.setup_xtream(
            xtream_base_url=sim_xtr.public_host, xtream_username=UPSTREAM_XTREAM_USER, xtream_password=UPSTREAM_XTREAM_PASS,
            provider_name=xtream_provider_name,
        )
        if not ok and "M3UNDLE_ENCRYPTION_KEY is not configured" in (client.last_setup_error or ""):
            original_encryption_key = lab_common.get_runtime_env_value(ENCRYPTION_KEY_ENV)
            restarted, detail = _restart_with_env(base, {ENCRYPTION_KEY_ENV: _generate_key()})
            if not restarted:
                state["reason"] = f"Could not restart with a temporary encryption key: {detail}"
                return {"state": state}
            restarted_encryption_key = True
            client = M3UndleClient(base)
            xtream_provider_name = f"provider-native-xtream-sim-restart-{int(time.time())}"
            ok = client.setup_xtream(
                xtream_base_url=sim_xtr.public_host, xtream_username=UPSTREAM_XTREAM_USER, xtream_password=UPSTREAM_XTREAM_PASS,
                provider_name=xtream_provider_name,
            )
        if not ok:
            state["reason"] = client.last_setup_error or "Native Xtream provider setup failed"
            return {"state": state}

        if not _enable_auth(client):
            state["reason"] = "Could not enable endpoint security for Xtream-backed output checks"
            return {"state": state}

        _test_prov_07(collector, client, expected_base_url=sim_xtr.public_host, expected_username=UPSTREAM_XTREAM_USER)
        _test_prov_08(collector, client, base)

        state["reason"] = None
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"Xtream suite setup failed: {exc}"
        return {"state": state}
    finally:
        try:
            if client:
                M3UndleClient(base)._request("PUT", "/api/v1/settings/endpoint-security", body={"enabled": False})
                client.clear_existing_providers()
        except Exception:
            pass
        for sim in (sim_xtr, sim_web_hls, sim_m3u):
            if sim is not None:
                try:
                    sim.stop()
                except Exception:
                    pass
        if restarted_encryption_key:
            if original_encryption_key is None:
                lab_common.unset_runtime_env_var(ENCRYPTION_KEY_ENV)
            else:
                lab_common.set_runtime_env_values({ENCRYPTION_KEY_ENV: original_encryption_key})
            try:
                lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
                wait_up(base, CONTAINER_NAME, health_paths=("/livez", "/health"))
            except Exception:
                pass


def _register_case(case_id: str) -> None:
    @SUITE.case(case_id)
    def _replay(ctx, state):
        records: dict[str, tuple[bool | None, str, Any]] = state["records"]  # type: ignore[assignment]
        entry = records.get(case_id)
        if entry is None:
            ctx.skip(case_id, str(state.get("reason") or "Xtream suite setup did not reach this case"))
            return
        passed, message, detail = entry
        ctx.record(case_id, passed, message, detail)


for _case_id in CASE_IDS:
    _register_case(_case_id)
