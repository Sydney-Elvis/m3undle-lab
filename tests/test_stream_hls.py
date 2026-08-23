"""Port of the frozen stream-HLS delivery suite, registered case-by-case with se-lab.

Covers the stable external HLS endpoint and the burst-buffering auto-redirect:

  HLS-AUTO-01  Burst-buffering UA -> compat /live/{streamKey} -> 302 to /hls/{streamKey}/index.m3u8
  HLS-AUTO-02  Burst-buffering UA -> Xtream /live/{u}/{p}/{id} -> 302 to /hls/{u}/{p}/{id}/index.m3u8
  HLS-MAN-01   GET /hls/{streamKey}/index.m3u8 returns valid M3U8 with /hls/generated/ segment URLs
  HLS-MAN-02   GET /hls/{u}/{p}/{id}/index.m3u8 returns valid M3U8 with /hls/generated/ segment URLs
  HLS-SEG-01   First segment from a compat HLS manifest contains MPEG-TS sync bytes (0x47)
"""

from __future__ import annotations

import json
import platform
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from agent.container import get_docker_gateway
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("stream-hls", group="core", order=120)
SIM_PORT = 19025
SIM_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-a.json"

XTREAM_USER = "hlstest"
XTREAM_PASS = "hlstest"

# Real Dalvik UA as sent by Android IPTV apps (the actual burst-buffering offender).
DALVIK_UA = "Dalvik/2.1.0 (Linux; U; Android 12; Hisense Build/STT1.211007.001)"

MANIFEST_POLL_RETRIES = 15
MANIFEST_POLL_INTERVAL = 2.0
TS_SYNC_BYTE = b"\x47"
TS_PACKET_SIZE = 188


def _simulator_address() -> tuple[str, str | None]:
    """Return the listener and advertised URL suitable for this Docker host."""
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{SIM_PORT}"

    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{SIM_PORT}"
    return "127.0.0.1", None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_301(self, req, fp, code, msg, headers):  # type: ignore[override]
        return fp
    http_error_302 = http_error_303 = http_error_307 = http_error_308 = http_error_301


def _probe_no_follow(url: str, user_agent: str) -> dict:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    try:
        with opener.open(req, timeout=15.0) as resp:
            status = getattr(resp, "status", resp.getcode())
            return {
                "status": status,
                "location": resp.headers.get("Location", ""),
                "content_type": resp.headers.get("Content-Type", ""),
            }
    except urllib.error.HTTPError as exc:
        return {
            "status": exc.code,
            "location": exc.headers.get("Location", "") if exc.headers else "",
            "content_type": exc.headers.get("Content-Type", "") if exc.headers else "",
        }
    except Exception as exc:
        return {"status": 0, "location": "", "content_type": "", "error": str(exc)}


def _fetch_manifest(url: str) -> tuple[int, str]:
    """Fetch an HLS manifest, retrying on 503 (FFmpeg still starting)."""
    for attempt in range(MANIFEST_POLL_RETRIES):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=15.0) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 503 and attempt < MANIFEST_POLL_RETRIES - 1:
                time.sleep(MANIFEST_POLL_INTERVAL)
                continue
            return exc.code, ""
        except Exception as exc:
            return 0, str(exc)
    return 503, ""


def _ts_framing_ok(data: bytes) -> bool:
    return (
        len(data) >= TS_PACKET_SIZE * 2
        and data[0:1] == TS_SYNC_BYTE
        and data[TS_PACKET_SIZE : TS_PACKET_SIZE + 1] == TS_SYNC_BYTE
    )


def _extract_stream_key(url: str) -> str | None:
    path = urlparse(url).path
    segments = [s for s in path.split("/") if s]
    try:
        idx = segments.index("live")
        key = segments[idx + 1]
        return key.rsplit(".", 1)[0] if "." in key else key
    except (ValueError, IndexError):
        return None


def _resolve_compat_manifest_url(stream_urls: list[str]) -> tuple[str | None, str | None]:
    """Follow the burst-buffering redirect from a /live/ URL. Returns (manifest_url, error)."""
    live_url = next((u for u in stream_urls if "/live/" in u), None)
    if not live_url:
        return None, f"no /live/ URL found in stream URLs (found: {stream_urls[:2]})"
    result = _probe_no_follow(live_url, DALVIK_UA)
    location = result.get("location", "")
    if result["status"] not in (301, 302, 303, 307, 308) or not location:
        return None, f"expected redirect, got status={result['status']} location={location!r}"
    parsed = urlparse(live_url)
    return urljoin(f"{parsed.scheme}://{parsed.netloc}", location), None


def _enable_auth(client: M3UndleClient) -> bool:
    status, _ = client._request(
        "PUT", "/api/v1/settings/endpoint-security",
        body={"enabled": True, "username": XTREAM_USER, "password": XTREAM_PASS},
    )
    return status in (200, 204)


def _disable_auth(client: M3UndleClient) -> bool:
    status, _ = client._request("PUT", "/api/v1/settings/endpoint-security", body={"enabled": False})
    return status in (200, 204)


def _get_xtream_stream_id(base: str) -> str | None:
    url = f"{base}/player_api.php?username={XTREAM_USER}&password={XTREAM_PASS}&action=get_live_streams"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10.0) as resp:
            streams = json.loads(resp.read())
            return str(streams[0]["stream_id"]) if isinstance(streams, list) and streams else None
    except Exception:
        return None


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    state: dict[str, object] = {
        "ready": False,
        "reason": "Stream-HLS suite setup did not complete",
        "client": None,
        "simulator": None,
        "base": base_url.rstrip("/"),
        "stream_urls": [],
    }
    try:
        bind, public_host = _simulator_address()
        simulator = SimulatorInstance(
            fixture=SIM_FIXTURE, port=SIM_PORT, bind=bind, public_host=public_host, suite="stream-hls",
        )
        state["simulator"] = simulator
        simulator.start()
        if not simulator.wait_healthy():
            state["reason"] = "Provider simulator did not become healthy"
            return {"state": state}

        client = M3UndleClient(state["base"])
        state["client"] = client
        if not client.setup(playlist_url=simulator.playlist_url, provider_name=f"hls-test-{int(time.time())}"):
            state["reason"] = client.last_setup_error or "M3Undle provider setup failed"
            return {"state": state}

        state["stream_urls"] = client.get_stream_urls()
        if not state["stream_urls"]:
            state["reason"] = "No stream URLs returned after provider setup"
            return {"state": state}

        state["ready"] = True
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"Stream-HLS suite setup failed: {exc}"
        return {"state": state}


def _ready(ctx: Any, state: dict[str, object], test_id: str) -> M3UndleClient | None:
    client = state.get("client")
    if state.get("ready") and isinstance(client, M3UndleClient):
        return client
    ctx.skip(test_id, str(state.get("reason") or "Stream-HLS suite setup did not complete"))
    return None


@SUITE.case("HLS-AUTO-01")
def hls_auto_01(ctx: Any, state: dict[str, object]) -> None:
    """A Dalvik UA on the compat /live/{streamKey} path receives a 302 to /hls/{streamKey}/index.m3u8."""
    if _ready(ctx, state, "HLS-AUTO-01") is None:
        return
    stream_urls: list[str] = state["stream_urls"]  # type: ignore[assignment]
    live_url = next((u for u in stream_urls if "/live/" in u), None)
    if not live_url:
        ctx.fail("HLS-AUTO-01", f"No /live/ URL in stream URLs — cannot test redirect (found: {stream_urls[:2]})")
        return

    stream_key = _extract_stream_key(live_url)
    if not stream_key:
        ctx.fail("HLS-AUTO-01", f"Could not extract stream key from {live_url!r}")
        return

    result = _probe_no_follow(live_url, DALVIK_UA)
    status = result["status"]
    location = result.get("location", "")
    redirected = status in (301, 302, 303, 307, 308)
    location_ok = f"/hls/{stream_key}/index.m3u8" in location

    ctx.record(
        "HLS-AUTO-01", redirected and location_ok,
        f"status={status} location={location!r} location_ok={location_ok}", detail=result,
    )


@SUITE.case("HLS-AUTO-02")
def hls_auto_02(ctx: Any, state: dict[str, object]) -> None:
    """A Dalvik UA on the Xtream /live/{u}/{p}/{id} path receives a 302 to /hls/{u}/{p}/{id}/index.m3u8."""
    client = _ready(ctx, state, "HLS-AUTO-02")
    if client is None:
        return
    base: str = state["base"]  # type: ignore[assignment]

    if not _enable_auth(client):
        ctx.fail("HLS-AUTO-02", "Could not enable endpoint-security for Xtream path test")
        return

    try:
        stream_id = _get_xtream_stream_id(base)
        if not stream_id:
            ctx.fail("HLS-AUTO-02", "get_live_streams returned no entries — Xtream API unreachable or auth wrong")
            return

        tune_url = f"{base}/live/{XTREAM_USER}/{XTREAM_PASS}/{stream_id}.ts"
        result = _probe_no_follow(tune_url, DALVIK_UA)
        status = result["status"]
        location = result.get("location", "")
        redirected = status in (301, 302, 303, 307, 308)
        location_ok = f"/hls/{XTREAM_USER}/{XTREAM_PASS}/{stream_id}/index.m3u8" in location

        ctx.record(
            "HLS-AUTO-02", redirected and location_ok,
            f"stream_id={stream_id} status={status} location={location!r} location_ok={location_ok}", detail=result,
        )
    finally:
        _disable_auth(client)


@SUITE.case("HLS-MAN-01")
def hls_man_01(ctx: Any, state: dict[str, object]) -> None:
    """GET /hls/{streamKey}/index.m3u8 returns a valid M3U8 playlist whose segment URLs point into /hls/generated/."""
    if _ready(ctx, state, "HLS-MAN-01") is None:
        return
    stream_urls: list[str] = state["stream_urls"]  # type: ignore[assignment]

    manifest_url, err = _resolve_compat_manifest_url(stream_urls)
    if not manifest_url:
        ctx.fail("HLS-MAN-01", f"Could not resolve manifest URL: {err}")
        return

    status, body = _fetch_manifest(manifest_url)
    if status != 200:
        ctx.fail("HLS-MAN-01", f"Manifest returned HTTP {status} after {MANIFEST_POLL_RETRIES} retries")
        return

    is_m3u8 = body.startswith("#EXTM3U")
    segment_lines = [ln.strip() for ln in body.splitlines() if ln.strip().endswith(".ts")]
    segments_rewritten = all("/hls/generated/" in ln for ln in segment_lines)

    ctx.record(
        "HLS-MAN-01", is_m3u8 and bool(segment_lines) and segments_rewritten,
        f"status={status} is_m3u8={is_m3u8} segment_count={len(segment_lines)} segments_rewritten={segments_rewritten}",
        detail={"segment_sample": segment_lines[:2]},
    )


@SUITE.case("HLS-MAN-02")
def hls_man_02(ctx: Any, state: dict[str, object]) -> None:
    """GET /hls/{u}/{p}/{id}/index.m3u8 returns a valid M3U8 playlist whose segment URLs point into /hls/generated/."""
    client = _ready(ctx, state, "HLS-MAN-02")
    if client is None:
        return
    base: str = state["base"]  # type: ignore[assignment]

    if not _enable_auth(client):
        ctx.fail("HLS-MAN-02", "Could not enable endpoint-security for Xtream manifest test")
        return

    try:
        stream_id = _get_xtream_stream_id(base)
        if not stream_id:
            ctx.fail("HLS-MAN-02", "get_live_streams returned no entries")
            return

        manifest_url = f"{base}/hls/{XTREAM_USER}/{XTREAM_PASS}/{stream_id}/index.m3u8"
        status, body = _fetch_manifest(manifest_url)
        if status != 200:
            ctx.fail("HLS-MAN-02", f"Xtream HLS manifest returned HTTP {status} after {MANIFEST_POLL_RETRIES} retries")
            return

        is_m3u8 = body.startswith("#EXTM3U")
        segment_lines = [ln.strip() for ln in body.splitlines() if ln.strip().endswith(".ts")]
        segments_rewritten = all("/hls/generated/" in ln for ln in segment_lines)

        ctx.record(
            "HLS-MAN-02", is_m3u8 and bool(segment_lines) and segments_rewritten,
            f"status={status} is_m3u8={is_m3u8} segment_count={len(segment_lines)} segments_rewritten={segments_rewritten}",
            detail={"segment_sample": segment_lines[:2]},
        )
    finally:
        _disable_auth(client)


@SUITE.case("HLS-SEG-01")
def hls_seg_01(ctx: Any, state: dict[str, object]) -> None:
    """The first segment from the compat HLS manifest is a valid MPEG-TS file (sync byte 0x47 at offsets 0 and 188)."""
    if _ready(ctx, state, "HLS-SEG-01") is None:
        return
    stream_urls: list[str] = state["stream_urls"]  # type: ignore[assignment]

    manifest_url, err = _resolve_compat_manifest_url(stream_urls)
    if not manifest_url:
        ctx.fail("HLS-SEG-01", f"Could not resolve manifest URL: {err}")
        return

    status, body = _fetch_manifest(manifest_url)
    if status != 200:
        ctx.fail("HLS-SEG-01", f"Manifest returned HTTP {status}; cannot derive segment URL")
        return

    segment_lines = [ln.strip() for ln in body.splitlines() if ln.strip().endswith(".ts")]
    if not segment_lines:
        ctx.fail("HLS-SEG-01", "No segment URLs found in manifest")
        return

    seg = segment_lines[0]
    segment_url = seg if seg.startswith("http") else urljoin(manifest_url, seg)

    try:
        with urllib.request.urlopen(urllib.request.Request(segment_url), timeout=15.0) as resp:
            seg_status = resp.status
            data = resp.read(TS_PACKET_SIZE * 3)
    except urllib.error.HTTPError as exc:
        ctx.fail("HLS-SEG-01", f"HTTP {exc.code} fetching segment {segment_url!r}")
        return
    except Exception as exc:
        ctx.fail("HLS-SEG-01", f"Error fetching segment: {exc}")
        return

    framing = _ts_framing_ok(data)
    ctx.record(
        "HLS-SEG-01", seg_status == 200 and framing,
        f"status={seg_status} bytes={len(data)} ts_sync_byte_at_0={data[:1] == TS_SYNC_BYTE} ts_framing={framing}",
        detail={"segment_url": segment_url, "first_bytes": data[:4].hex() if data else ""},
    )


@SUITE.teardown
def teardown(state: dict[str, object]) -> None:
    client = state.get("client")
    if isinstance(client, M3UndleClient) and client.provider_id:
        try:
            client.delete_provider(client.provider_id)
        except Exception:
            pass

    simulator = state.get("simulator")
    if isinstance(simulator, SimulatorInstance):
        try:
            simulator.stop()
        except Exception:
            pass
