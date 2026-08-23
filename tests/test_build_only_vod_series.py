"""Build-only VOD and series persistence after an application restart."""

from __future__ import annotations

import base64
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

from agent import common as lab_common
from agent.container import container_started_at, get_docker_gateway, wait_for_restart, wait_up
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.commands import CONTAINER_NAME, HOST_OVERRIDE
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("build-vod-series", group="core", order=160)
SIM_PORT = 19019
SIM_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-xtream.json"
UPSTREAM_XTREAM_USER = "xtreamuser"
UPSTREAM_XTREAM_PASS = "xtreampass"
XTREAM_MOVIE_TITLE = "Lab Movie One"
XTREAM_SERIES_TITLE = "Lab Show One"
M3U_PLAYLIST = """#EXTM3U
#EXTINF:-1 group-title="Lab Movies",Lab M3U Movie
http://example.invalid/movie/user/pass/900.mkv
#EXTINF:-1 group-title="Lab Series",Lab M3U Show - S01E01
http://example.invalid/series/user/pass/901.mkv
"""
M3U_MOVIE_TITLE = "Lab M3U Movie"
M3U_EPISODE_TITLE = "Lab M3U Show"
ENCRYPTION_KEY_ENV = "M3UNDLE_ENCRYPTION_KEY"
ENCRYPTION_KEYS_ENV = "M3UNDLE_ENCRYPTION_KEYS"
TEMP_KEY = base64.b64encode(b"m3undle-lab-build-vod-series-key").decode("ascii")
STARTUP_SETTLE_SECONDS = 35.0


def _simulator_address() -> tuple[str, str | None]:
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{SIM_PORT}"
    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{SIM_PORT}"
    return "127.0.0.1", None


def _restart_and_wait(base_url: str) -> tuple[bool, str]:
    previous_started_at = container_started_at(CONTAINER_NAME)
    result = subprocess.run(
        ["docker", "restart", CONTAINER_NAME], capture_output=True, text=True, timeout=30.0, check=False
    )
    if result.returncode != 0:
        return False, f"docker restart failed: {result.stderr.strip()[:200]}"
    if not wait_for_restart(CONTAINER_NAME, previous_started_at, timeout=60.0):
        return False, "container did not report a new start timestamp"
    if not wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health")):
        return False, "container did not become healthy after restart"
    # The refresh service has a fixed startup trigger-processing delay. A
    # build fired before it clears can falsely look settled without executing.
    time.sleep(STARTUP_SETTLE_SECONDS)
    return True, "restarted, healthy, and past the startup trigger window"


def _restore_environment(base_url: str, state: dict[str, object]) -> None:
    original = state.get("original_env")
    if not isinstance(original, dict):
        return
    updates: dict[str, str] = {}
    for key, value in original.items():
        if value is None:
            lab_common.unset_runtime_env_var(str(key))
        else:
            updates[str(key)] = str(value)
    if updates:
        lab_common.write_env_file_values(lab_common.runtime_env_file(), updates)
    lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
    wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health"))


def _rebuild_m3u(client: M3UndleClient, profile_id: str) -> tuple[int, str]:
    client.build_snapshot()
    if not client.poll_build_completion(profile_id):
        raise RuntimeError("Build-only snapshot did not complete")
    status, body = client.get("/m3u/m3undle.m3u")
    return status, body if isinstance(body, str) else ""


def _ready(ctx: Any, state: dict[str, object], test_id: str) -> M3UndleClient | None:
    client = state.get("client")
    if state.get("ready") and isinstance(client, M3UndleClient):
        return client
    ctx.skip(test_id, str(state.get("reason") or "Build VOD/series setup did not complete"))
    return None


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    state: dict[str, object] = {
        "ready": False,
        "reason": "Build VOD/series setup did not complete",
        "client": None,
        "simulator": None,
        "original_env": None,
    }
    try:
        current_env = lab_common.load_env_file(lab_common.runtime_env_file())
        original = {key: current_env.get(key) for key in (ENCRYPTION_KEY_ENV, ENCRYPTION_KEYS_ENV)}
        state["original_env"] = original
        if not original[ENCRYPTION_KEY_ENV] and not original[ENCRYPTION_KEYS_ENV]:
            lab_common.set_runtime_env_values({ENCRYPTION_KEY_ENV: TEMP_KEY})
            lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
            if not wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health")):
                state["reason"] = "M3Undle did not become healthy with the temporary encryption key"
                return {"state": state}
        bind, public_host = _simulator_address()
        simulator = SimulatorInstance(
            fixture=SIM_FIXTURE, port=SIM_PORT, bind=bind, public_host=public_host, suite="build-vod-series"
        )
        state["simulator"] = simulator
        simulator.start()
        if not simulator.wait_healthy():
            state["reason"] = "Xtream simulator did not become healthy"
            return {"state": state}
        state["client"] = M3UndleClient(base_url)
        state["ready"] = True
    except Exception as exc:
        state["reason"] = f"Build VOD/series suite setup failed: {exc}"
    return {"state": state}


@SUITE.case("BUILD-VOD-01")
def build_vod_01(ctx: Any, base_url: str, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "BUILD-VOD-01")
    simulator = state.get("simulator")
    if client is None or not isinstance(simulator, SimulatorInstance):
        return
    upstream = simulator.public_host or simulator.playlist_url.rsplit("/playlist.m3u", 1)[0]
    try:
        if not client.setup_xtream(
            xtream_base_url=upstream,
            xtream_username=UPSTREAM_XTREAM_USER,
            xtream_password=UPSTREAM_XTREAM_PASS,
            provider_name="build-vod-series-xtream-sim",
            include_vod=True,
            include_series=True,
        ):
            raise RuntimeError(client.last_setup_error or "Xtream setup failed")
        profile_id = str(client.profile_id or "")
        if not profile_id:
            raise RuntimeError("Xtream setup did not return a profile")
        status, initial = client.get("/m3u/m3undle.m3u")
        initial_m3u = initial if status == 200 and isinstance(initial, str) else ""
        episodes_before = initial_m3u.count(XTREAM_SERIES_TITLE)
        if XTREAM_MOVIE_TITLE not in initial_m3u or episodes_before < 2:
            raise RuntimeError(
                f"Setup did not publish expected content: movie={XTREAM_MOVIE_TITLE in initial_m3u} episodes={episodes_before}"
            )
        restarted, detail = _restart_and_wait(base_url)
        if not restarted:
            raise RuntimeError(detail)
        rebuilt_status, rebuilt_m3u = _rebuild_m3u(client, profile_id)
        movie_after = XTREAM_MOVIE_TITLE in rebuilt_m3u
        episodes_after = rebuilt_m3u.count(XTREAM_SERIES_TITLE)
        valid = rebuilt_status == 200 and movie_after
        ctx.record("BUILD-VOD-01", valid, f"movie after restart+build-only={movie_after}; status={rebuilt_status}")
        state["xtream_series_ready"] = valid and episodes_after >= 2
        state["xtream_series_before"] = episodes_before
        state["xtream_series_after"] = episodes_after
    except Exception as exc:
        ctx.fail("BUILD-VOD-01", str(exc))


@SUITE.case("BUILD-SERIES-01")
def build_series_01(ctx: Any, state: dict[str, object]) -> None:
    if not state.get("xtream_series_ready"):
        ctx.skip("BUILD-SERIES-01", "Skipped because the Xtream VOD setup/build did not complete")
        return
    before = int(state.get("xtream_series_before") or 0)
    after = int(state.get("xtream_series_after") or 0)
    ctx.record("BUILD-SERIES-01", after >= 2, f"series episode lines after restart+build-only={after}; before={before}")


@SUITE.case("BUILD-SERIES-M3U-01")
def build_series_m3u_01(ctx: Any, base_url: str, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "BUILD-SERIES-M3U-01")
    if client is None:
        return
    try:
        playlist_name = "build-series-m3u.m3u"
        m3u_dir = lab_common.runtime_dir() / "m3u_data"
        m3u_dir.mkdir(parents=True, exist_ok=True)
        (m3u_dir / playlist_name).write_text(M3U_PLAYLIST, encoding="utf-8")
        playlist_url = f"file:///m3u_data/{playlist_name}"
        provider_name = f"provider-m3u-series-{int(time.time())}"
        if not client.setup(playlist_url=playlist_url, provider_name=provider_name):
            raise RuntimeError(client.last_setup_error or "M3U setup failed")
        provider_id = str(client.provider_id or "")
        profile_id = str(client.profile_id or "")
        if not provider_id or not profile_id:
            raise RuntimeError("M3U setup did not return a provider and profile")
        status, body = client._request(
            "PUT",
            f"/api/v1/providers/{provider_id}",
            body={
                "name": provider_name,
                "playlistUrl": playlist_url,
                "enabled": True,
                "includeVod": True,
                "includeSeries": True,
                "timeoutSeconds": 120,
                "maxConcurrentStreams": 2,
                "associateToProfileIds": [profile_id],
            },
        )
        if status != 200:
            raise RuntimeError(f"Enabling VOD/series failed: status={status} body={body}")
        previous_started_utc = str(client.get_snapshot_status_info().get("startedUtc") or "")
        client.trigger_refresh()
        if not client.poll_snapshot_status(previous_started_utc=previous_started_utc):
            raise RuntimeError("Refresh after enabling VOD/series did not complete")
        status, initial = client.get("/m3u/m3undle.m3u")
        initial_m3u = initial if status == 200 and isinstance(initial, str) else ""
        if M3U_MOVIE_TITLE not in initial_m3u or M3U_EPISODE_TITLE not in initial_m3u:
            raise RuntimeError(
                f"Initial fetch did not publish expected content: movie={M3U_MOVIE_TITLE in initial_m3u} "
                f"episode={M3U_EPISODE_TITLE in initial_m3u}"
            )
        restarted, detail = _restart_and_wait(base_url)
        if not restarted:
            raise RuntimeError(detail)
        rebuilt_status, rebuilt_m3u = _rebuild_m3u(client, profile_id)
        movie_after = M3U_MOVIE_TITLE in rebuilt_m3u
        episode_after = M3U_EPISODE_TITLE in rebuilt_m3u
        ctx.record(
            "BUILD-SERIES-M3U-01",
            rebuilt_status == 200 and movie_after and episode_after,
            f"movie_after={movie_after} episode_after={episode_after} status={rebuilt_status}",
        )
    except Exception as exc:
        ctx.fail("BUILD-SERIES-M3U-01", str(exc))


@SUITE.teardown
def teardown(base_url: str, state: dict[str, object]) -> None:
    client = state.get("client")
    if isinstance(client, M3UndleClient):
        try:
            client.clear_existing_providers()
        except Exception:
            pass
    try:
        _restore_environment(base_url, state)
    except Exception:
        pass
    simulator = state.get("simulator")
    if isinstance(simulator, SimulatorInstance):
        try:
            simulator.stop()
        except Exception:
            pass
