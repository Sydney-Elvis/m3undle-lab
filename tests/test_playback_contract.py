"""Port of the frozen black-box live/VOD/series playback-contract suite.

Simplification versus the frozen script: the original is an imperative script
with two early-abort points (no VOD/series after discovery; no episodes after
series-info lookup) that leave some case IDs unrecorded on certain failure
paths. agent.suites requires every declared case to record exactly once, so
this port treats the whole setup sequence (client config, discovery, movie/
series/episode resolution) as one atomic readiness gate -- same pattern
already established by test_security.py/test_encryption_rotation.py: every
case is skipped uniformly if setup didn't reach "ready", rather than some
cases recording and others silently never appearing depending on exactly
where the original script aborted. The assertions themselves are unchanged.

Also replaces the frozen suite's role-gated (srv1-only) temporary-encryption-
key restart, borrowed from test_xtream.py, with the same role-free
_restart_with_env pattern test_encryption_rotation.py already uses -- se-lab
dropped the srv1/srv2 role concept entirely this migration.
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from agent import common as lab_common
from agent.container import get_docker_gateway, wait_up
from agent.suites import suite

from m3undle_lab.api import HoldOpenStream, M3UndleClient
from m3undle_lab.commands import CONTAINER_NAME, HOST_OVERRIDE
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("playback-contract", group="core", order=130)
SIM_PORT = 19019
SIM_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-xtream.json"
UPSTREAM_USER = "xtreamuser"
UPSTREAM_PASS = "xtreampass"
CLIENT_USER = "testuser"
CLIENT_PASS = "testpass"
IPTVNATOR_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) IPTVnator/0.16.0 Chrome/120.0 Electron/28.1.0"
SMARTERS_UA = "okhttp/4.12.0 SmartersPro/1.0 (Android TV)"
ENCRYPTION_KEY_ENV = "M3UNDLE_ENCRYPTION_KEY"

MEDIA_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "media"
PLAYBACK_MEDIA = {
    "movie:30001": "playback-movie.mkv",
    "series:50001": "playback-episode.mkv",
}


def _playback_media(marker: str) -> bytes:
    return (MEDIA_DIR / PLAYBACK_MEDIA[marker]).read_bytes()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _api_json(base: str, action: str, **query: str) -> tuple[int, object]:
    params = {"username": CLIENT_USER, "password": CLIENT_PASS, "action": action, **query}
    url = f"{base}/player_api.php?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, {}


def _probe(url: str, *, user_agent: str, byte_range: str | None = None, if_range: str | None = None) -> dict:
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    if byte_range:
        headers["Range"] = byte_range
    if if_range:
        headers["If-Range"] = if_range
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    started = time.monotonic()
    try:
        with opener.open(request, timeout=20) as response:
            body = response.read()
            return {
                "status": response.status,
                "headers": {key.lower(): value for key, value in response.headers.items()},
                "body": body,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            }
    except urllib.error.HTTPError as error:
        return {
            "status": error.code,
            "headers": {key.lower(): value for key, value in error.headers.items()},
            "body": error.read(),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        }
    except Exception as error:
        return {"status": 0, "headers": {}, "body": b"", "error": str(error), "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}


def _fetch_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


def _probe_status(url: str, user_agent: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read(1)
            return {"status": response.status, "headers": {key.lower(): value for key, value in response.headers.items()}}
    except urllib.error.HTTPError as error:
        try:
            return {"status": error.code, "headers": {key.lower(): value for key, value in error.headers.items()}}
        finally:
            error.close()
    except Exception as error:
        return {"status": 0, "headers": {}, "error": str(error)}


def _stream_url(base: str, kind: str, stream_id: int, extension: str = "mkv") -> str:
    return f"{base}/{kind}/{CLIENT_USER}/{CLIENT_PASS}/{stream_id}.{extension}"


def _compatibility_urls(base: str) -> dict[str, str]:
    url = f"{base}/m3u/m3undle.m3u?{urllib.parse.urlencode({'username': CLIENT_USER, 'password': CLIENT_PASS})}"
    with urllib.request.urlopen(url, timeout=15) as response:
        lines = response.read().decode("utf-8", errors="replace").splitlines()
    result: dict[str, str] = {}
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("#EXTINF"):
            continue
        title = line.rsplit(",", 1)[-1]
        if title == "Lab Movie One":
            result["movie"] = lines[index + 1]
        elif "S01E01" in title:
            result["series"] = lines[index + 1]
    return result


def _ondemand_event_count(sim_local_url: str) -> int:
    state = _fetch_json(f"{sim_local_url}/debug/state")
    events = state.get("recent_events", []) if isinstance(state, dict) else []
    return sum(1 for event in events if event.get("event") == "ondemand_served")


def _enable_auth(client: M3UndleClient) -> bool:
    status, _ = client._request(
        "PUT", "/api/v1/settings/endpoint-security",
        body={"enabled": True, "username": CLIENT_USER, "password": CLIENT_PASS},
    )
    return status in (200, 204)


def _simulator_address() -> tuple[str, str | None]:
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{SIM_PORT}"
    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{SIM_PORT}"
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
# Setup: bring up the simulator + a native Xtream provider with VOD/series,
# resolve every id/url every case needs, gated behind one "ready" flag.
# ---------------------------------------------------------------------------

@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    base = base_url.rstrip("/")
    state: dict[str, object] = {
        "ready": False,
        "reason": "Playback-contract suite setup did not complete",
        "client": None,
        "simulator": None,
        "base": base,
        "restarted_encryption_key": False,
        "original_encryption_key": None,
    }

    bind, public_host = _simulator_address()
    simulator = SimulatorInstance(fixture=SIM_FIXTURE, port=SIM_PORT, bind=bind, public_host=public_host, suite="playback-contract")
    state["simulator"] = simulator
    try:
        simulator.start()
        if not simulator.wait_healthy():
            state["reason"] = "Provider simulator did not become healthy"
            return {"state": state}

        client = M3UndleClient(base)
        state["client"] = client
        ok = client.setup_xtream(
            xtream_base_url=simulator.public_host,
            xtream_username=UPSTREAM_USER,
            xtream_password=UPSTREAM_PASS,
            include_vod=True,
            include_series=True,
            max_concurrent_streams=1,
            provider_name=f"provider-playback-contract-{int(time.time())}",
        )
        if not ok and "M3UNDLE_ENCRYPTION_KEY is not configured" in (client.last_setup_error or ""):
            state["original_encryption_key"] = lab_common.get_runtime_env_value(ENCRYPTION_KEY_ENV)
            restarted, detail = _restart_with_env(base, {ENCRYPTION_KEY_ENV: _generate_key()})
            if not restarted:
                state["reason"] = f"Could not restart with a temporary encryption key: {detail}"
                return {"state": state}
            state["restarted_encryption_key"] = True
            client = M3UndleClient(base)
            state["client"] = client
            ok = client.setup_xtream(
                xtream_base_url=simulator.public_host,
                xtream_username=UPSTREAM_USER,
                xtream_password=UPSTREAM_PASS,
                include_vod=True,
                include_series=True,
                max_concurrent_streams=1,
                provider_name=f"provider-playback-contract-restart-{int(time.time())}",
            )
        if not ok or not _enable_auth(client):
            state["reason"] = client.last_setup_error or "Could not configure native Xtream provider/auth"
            return {"state": state}

        live_status, live = _api_json(base, "get_live_streams")
        movie_status, movies = _api_json(base, "get_vod_streams")
        series_status, series = _api_json(base, "get_series")
        state["disc01"] = (live_status == movie_status == series_status == 200 and bool(live) and bool(movies) and bool(series), len(live) if isinstance(live, list) else 0, len(movies) if isinstance(movies, list) else 0, len(series) if isinstance(series, list) else 0)
        if not isinstance(movies, list) or not movies or not isinstance(series, list) or not series or not isinstance(live, list) or not live:
            state["reason"] = "Discovery did not return live channels, VOD movies, and series together"
            return {"state": state}
        state["live_stream_id"] = int(live[0]["stream_id"])

        movie = next(item for item in movies if item.get("name") == "Lab Movie One")
        cap_movie = next(item for item in movies if item.get("name") == "Lab Movie Cap Hold")
        published_series = series[0]
        series_id = str(published_series["series_id"])
        state["disc02"] = (bool(movie.get("stream_id")) and bool(series_id), movie.get("name"), published_series.get("name"))

        info_status, info = _api_json(base, "get_series_info", series_id=series_id)
        episodes = info.get("episodes", {}).get("1", []) if isinstance(info, dict) else []
        state["ser01"] = (info_status == 200 and len(episodes) >= 2, info_status, len(episodes))
        if len(episodes) < 2:
            state["reason"] = "Series info did not return at least two season-1 episodes"
            return {"state": state}

        movie_id = int(movie["stream_id"])
        episode_id = int(episodes[0]["id"])
        cap_movie_id = int(cap_movie["stream_id"])
        cap_episode_id = int(episodes[1]["id"])
        cap_live = live[1] if len(live) > 1 else live[0]

        state.update({
            "movie_url": _stream_url(base, "movie", movie_id),
            "episode_url": _stream_url(base, "series", episode_id),
            "cap_movie_url": _stream_url(base, "movie", cap_movie_id),
            "cap_episode_url": _stream_url(base, "series", cap_episode_id),
            "live_url": f"{base}/live/{CLIENT_USER}/{CLIENT_PASS}/{int(cap_live['stream_id'])}.ts",
            "sim_local_url": simulator._local_url,
            "ready": True,
        })
        state["reason"] = None
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"Playback-contract suite setup failed: {exc}"
        return {"state": state}


def _ready(ctx: Any, state: dict[str, object], test_id: str) -> bool:
    if state.get("ready"):
        return True
    ctx.skip(test_id, str(state.get("reason") or "Playback-contract suite setup did not complete"))
    return False


# ---------------------------------------------------------------------------
# Discovery cases
# ---------------------------------------------------------------------------

@SUITE.case("PLAY-DISC-01")
def play_disc_01(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-DISC-01"):
        return
    passed, live_count, movie_count, series_count = state["disc01"]  # type: ignore[misc]
    ctx.record("PLAY-DISC-01", passed, f"live={live_count} movies={movie_count} series={series_count}")


@SUITE.case("PLAY-LIVE-01")
def play_live_01(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-LIVE-01"):
        return
    base: str = state["base"]  # type: ignore[assignment]
    url = f"{base}/live/{CLIENT_USER}/{CLIENT_PASS}/{state['live_stream_id']}.ts"
    request = urllib.request.Request(url, headers={"User-Agent": IPTVNATOR_UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(940)
            framed = len(body) == 940 and all(body[offset] == 0x47 for offset in range(0, 940, 188))
            ctx.record("PLAY-LIVE-01", response.status == 200 and framed, f"status={response.status} bytes={len(body)} ts_framed={framed}")
    except Exception as error:
        ctx.fail("PLAY-LIVE-01", f"live probe failed: {error}")


@SUITE.case("PLAY-DISC-02")
def play_disc_02(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-DISC-02"):
        return
    passed, movie_name, series_name = state["disc02"]  # type: ignore[misc]
    ctx.record("PLAY-DISC-02", passed, f"movie_name={movie_name!r} series_name={series_name!r}")


@SUITE.case("PLAY-SER-01")
def play_ser_01(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-SER-01"):
        return
    passed, info_status, episode_count = state["ser01"]  # type: ignore[misc]
    ctx.record("PLAY-SER-01", passed, f"status={info_status} episodes={episode_count}")


# ---------------------------------------------------------------------------
# Playback cases
# ---------------------------------------------------------------------------

def _record_full(ctx: Any, test_id: str, url: str, marker: str, user_agent: str) -> None:
    result = _probe(url, user_agent=user_agent)
    expected = _playback_media(marker)
    passed = (
        result["status"] == 200
        and result["headers"].get("content-type", "").startswith("video/")
        and result["body"] == expected
        and not result["headers"].get("location")
    )
    digest = hashlib.sha256(result["body"]).hexdigest()
    ctx.record(
        test_id, passed,
        f"status={result['status']} bytes={len(result['body'])} sha256={digest[:16]}",
        detail={
            "route_class": urllib.parse.urlsplit(url).path.split("/")[1],
            "status": result["status"],
            "response_headers": result["headers"],
            "bytes_received": len(result["body"]),
            "sha256": digest,
            "fixture_marker": marker,
            "elapsed_ms": result["elapsed_ms"],
            "redirected": bool(result["headers"].get("location")),
        },
    )


def _record_range_matrix(ctx: Any, test_id: str, url: str, marker: str, user_agent: str) -> None:
    payload = _playback_media(marker)
    total = len(payload)
    final_start = total - 1024
    cases = (
        ("full", None, None, 200, payload, None),
        ("closed", "bytes=65536-66559", None, 206, payload[65536:66560], f"bytes 65536-66559/{total}"),
        ("open", f"bytes={final_start}-", None, 206, payload[-1024:], f"bytes {final_start}-{total - 1}/{total}"),
        ("suffix", "bytes=-1024", None, 206, payload[-1024:], f"bytes {final_start}-{total - 1}/{total}"),
        ("if-range-valid", "bytes=0-1023", f'"lab-{marker.replace(":", "-")}-v1"', 206, payload[:1024], f"bytes 0-1023/{total}"),
        ("if-range-stale", "bytes=0-1023", '"stale"', 200, payload, None),
        ("unsatisfiable", f"bytes={total}-", None, 416, b"", f"bytes */{total}"),
    )
    failures = []
    details = []
    for name, byte_range, if_range, status, expected, content_range in cases:
        result = _probe(url, user_agent=user_agent, byte_range=byte_range, if_range=if_range)
        headers = result["headers"]
        valid = result["status"] == status and result["body"] == expected
        if status == 206:
            valid = valid and headers.get("accept-ranges") == "bytes" and headers.get("content-length") == str(len(expected))
        if content_range is not None:
            valid = valid and headers.get("content-range") == content_range
        if not valid:
            failures.append(name)
        details.append(f"{name}:{result['status']}/{len(result['body'])}/{headers.get('content-range')}")
    ctx.record(test_id, not failures, f"failures={failures or 'none'} {'; '.join(details)}")


@SUITE.case("PLAY-VOD-01")
def play_vod_01(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-VOD-01"):
        return
    _record_full(ctx, "PLAY-VOD-01", state["movie_url"], "movie:30001", IPTVNATOR_UA)  # type: ignore[arg-type]


@SUITE.case("PLAY-VOD-02")
def play_vod_02(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-VOD-02"):
        return
    _record_range_matrix(ctx, "PLAY-VOD-02", state["movie_url"], "movie:30001", IPTVNATOR_UA)  # type: ignore[arg-type]


@SUITE.case("PLAY-SER-02")
def play_ser_02(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-SER-02"):
        return
    _record_full(ctx, "PLAY-SER-02", state["episode_url"], "series:50001", SMARTERS_UA)  # type: ignore[arg-type]


@SUITE.case("PLAY-SER-03")
def play_ser_03(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-SER-03"):
        return
    _record_range_matrix(ctx, "PLAY-SER-03", state["episode_url"], "series:50001", SMARTERS_UA)  # type: ignore[arg-type]


@SUITE.case("PLAY-COMPAT-00")
def play_compat_00(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-COMPAT-00"):
        return
    base: str = state["base"]  # type: ignore[assignment]
    compat = _compatibility_urls(base)
    state["compat"] = compat
    ctx.record("PLAY-COMPAT-00", set(compat) == {"movie", "series"}, f"routes={sorted(compat)}")


@SUITE.case("PLAY-COMPAT-01")
def play_compat_01(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-COMPAT-01"):
        return
    compat = state.get("compat")
    if not isinstance(compat, dict) or "movie" not in compat:
        ctx.fail("PLAY-COMPAT-01", "PLAY-COMPAT-00 did not resolve a compat movie route")
        return
    _record_range_matrix(ctx, "PLAY-COMPAT-01", compat["movie"], "movie:30001", IPTVNATOR_UA)


@SUITE.case("PLAY-COMPAT-02")
def play_compat_02(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-COMPAT-02"):
        return
    compat = state.get("compat")
    if not isinstance(compat, dict) or "series" not in compat:
        ctx.fail("PLAY-COMPAT-02", "PLAY-COMPAT-00 did not resolve a compat series route")
        return
    _record_range_matrix(ctx, "PLAY-COMPAT-02", compat["series"], "series:50001", SMARTERS_UA)


def _record_cap(ctx: Any, test_id: str, held_url: str, blocked_requests: list[tuple[str, str]], retry_url: str, sim_local_url: str) -> None:
    holder = HoldOpenStream(held_url, hold_seconds=10, chunk_size=4096)
    holder.start()
    header_status = holder.wait_header(timeout_seconds=10)
    deadline = time.monotonic() + 5
    while holder.bytes_read == 0 and not holder.finished and time.monotonic() < deadline:
        time.sleep(0.05)
    before_events = _ondemand_event_count(sim_local_url)
    blocked = [_probe_status(url, user_agent) for url, user_agent in blocked_requests]
    after_events = _ondemand_event_count(sim_local_url)
    holder.stop()

    retry = {"status": 0}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        retry = _probe(retry_url, user_agent=SMARTERS_UA, byte_range="bytes=0-0")
        if retry["status"] == 206:
            break
        time.sleep(0.1)

    blocked_statuses = [item["status"] for item in blocked]
    retry_after_valid = all(str(item["headers"].get("retry-after", "")).isdigit() for item in blocked)
    passed = (
        header_status == 200
        and holder.bytes_read > 0
        and all(status == 503 for status in blocked_statuses)
        and retry_after_valid
        and after_events == before_events
        and retry["status"] == 206
    )
    ctx.record(
        test_id, passed,
        f"held={header_status}/{holder.bytes_read} blocked={blocked_statuses} retry_after={retry_after_valid} "
        f"upstream_events={before_events}->{after_events} released_retry={retry['status']}",
    )


@SUITE.case("PLAY-CAP-01")
def play_cap_01(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-CAP-01"):
        return
    _record_cap(
        ctx, "PLAY-CAP-01", state["cap_movie_url"],  # type: ignore[arg-type]
        [(state["episode_url"], SMARTERS_UA), (state["live_url"], IPTVNATOR_UA)],  # type: ignore[list-item]
        state["episode_url"], state["sim_local_url"],  # type: ignore[arg-type]
    )


@SUITE.case("PLAY-CAP-02")
def play_cap_02(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-CAP-02"):
        return
    _record_cap(
        ctx, "PLAY-CAP-02", state["cap_episode_url"],  # type: ignore[arg-type]
        [(state["movie_url"], SMARTERS_UA), (state["live_url"], IPTVNATOR_UA)],  # type: ignore[list-item]
        state["movie_url"], state["sim_local_url"],  # type: ignore[arg-type]
    )


def _record_monitor(ctx: Any, base: str, url: str, title: str, test_id: str) -> None:
    outcome: dict = {}

    def consume() -> None:
        outcome.update(_probe(url, user_agent=IPTVNATOR_UA))

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    session_visible = client_visible = provider_visible = counters_increased = False
    session_id = ""
    first_session_bytes = first_client_bytes = second_session_bytes = second_client_bytes = 0
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and thread.is_alive():
        try:
            summary = _fetch_json(f"{base}/status/streams")
            clients = _fetch_json(f"{base}/status/streams/clients")
            providers = _fetch_json(f"{base}/status/streams/providers")
            sessions = summary.get("activeSessions", []) if isinstance(summary, dict) else []
            match = next((item for item in sessions if title.lower() in str(item.get("displayName", "")).lower()), None)
            if match:
                session_id = str(match.get("sessionId", ""))
                client = next((item for item in clients if str(item.get("sessionId", "")) == session_id), None)
                provider = next((item for item in providers if str(item.get("sessionId", "")) == session_id), None)
                session_visible = bool(session_id)
                client_visible = client is not None
                provider_visible = provider is not None
                session_bytes = int(match.get("totalBytesRelayed", 0) or 0)
                client_bytes = int(client.get("bytesSent", 0) or 0) if client else 0
                if first_session_bytes == 0 and session_bytes > 0 and client_bytes > 0:
                    first_session_bytes, first_client_bytes = session_bytes, client_bytes
                elif first_session_bytes > 0:
                    second_session_bytes, second_client_bytes = session_bytes, client_bytes
                    counters_increased = second_session_bytes > first_session_bytes and second_client_bytes > first_client_bytes
        except Exception:
            pass
        if session_visible and client_visible and provider_visible and counters_increased:
            break
        time.sleep(0.05)
    thread.join(timeout=10)

    cleaned_up = clients_cleaned_up = providers_cleaned_up = recently_ended = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            summary = _fetch_json(f"{base}/status/streams")
            clients = _fetch_json(f"{base}/status/streams/clients")
            providers = _fetch_json(f"{base}/status/streams/providers")
            active = summary.get("activeSessions", []) if isinstance(summary, dict) else []
            recent = summary.get("recentEndedSessions", []) if isinstance(summary, dict) else []
            cleaned_up = bool(session_id) and all(str(item.get("sessionId", "")) != session_id for item in active)
            clients_cleaned_up = bool(session_id) and all(str(item.get("sessionId", "")) != session_id for item in clients)
            providers_cleaned_up = bool(session_id) and all(str(item.get("sessionId", "")) != session_id for item in providers)
            recently_ended = bool(session_id) and any(str(item.get("sessionId", "")) == session_id for item in recent)
        except Exception:
            pass
        if cleaned_up and clients_cleaned_up and providers_cleaned_up and recently_ended:
            break
        time.sleep(0.05)

    passed = (
        session_visible and client_visible and provider_visible and counters_increased
        and cleaned_up and clients_cleaned_up and providers_cleaned_up and recently_ended
        and outcome.get("status") == 200
    )
    ctx.record(
        test_id, passed,
        f"session={session_visible} client={client_visible} provider={provider_visible} "
        f"bytes={first_session_bytes}->{second_session_bytes}/{first_client_bytes}->{second_client_bytes} "
        f"cleanup={cleaned_up}/{clients_cleaned_up}/{providers_cleaned_up} recent={recently_ended} "
        f"playback_status={outcome.get('status', 0)}",
        detail={
            "session_id": session_id,
            "session_bytes": [first_session_bytes, second_session_bytes],
            "client_bytes": [first_client_bytes, second_client_bytes],
            "active_cleanup": {"session": cleaned_up, "client": clients_cleaned_up, "provider": providers_cleaned_up},
            "recently_ended": recently_ended,
            "playback_status": outcome.get("status", 0),
        },
    )


@SUITE.case("PLAY-MON-01")
def play_mon_01(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-MON-01"):
        return
    _record_monitor(ctx, state["base"], state["cap_movie_url"], "Lab Movie Cap Hold", "PLAY-MON-01")  # type: ignore[arg-type]


@SUITE.case("PLAY-MON-02")
def play_mon_02(ctx: Any, state: dict[str, object]) -> None:
    if not _ready(ctx, state, "PLAY-MON-02"):
        return
    _record_monitor(ctx, state["base"], state["cap_episode_url"], "S01E02", "PLAY-MON-02")  # type: ignore[arg-type]


@SUITE.teardown
def teardown(state: dict[str, object]) -> None:
    client = state.get("client")
    if isinstance(client, M3UndleClient):
        try:
            client._request("PUT", "/api/v1/settings/endpoint-security", body={"enabled": False})
            client.clear_existing_providers()
        except Exception:
            pass

    simulator = state.get("simulator")
    if isinstance(simulator, SimulatorInstance):
        try:
            simulator.stop()
        except Exception:
            pass

    if state.get("restarted_encryption_key"):
        base: str = state["base"]  # type: ignore[assignment]
        original = state.get("original_encryption_key")
        if original is None:
            lab_common.unset_runtime_env_var(ENCRYPTION_KEY_ENV)
        else:
            lab_common.set_runtime_env_values({ENCRYPTION_KEY_ENV: str(original)})
        try:
            lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
            wait_up(base, CONTAINER_NAME, health_paths=("/livez", "/health"))
        except Exception:
            pass
