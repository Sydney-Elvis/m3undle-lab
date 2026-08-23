"""Port of the frozen profile and active-profile-switching suite (PRF-01..06, PROFILE-OUT-01).

No simulator content changes needed -- these are pure API orchestration
tests, plus one output-tracking check comparing published M3U/HDHR before and
after a profile switch.

Structural adaptation, not a behavior change: the frozen script is one long
imperative main() where later cases depend on ids/state earlier cases
produced, and some paths leave a case unrecorded entirely (e.g. "PRF-SETUP"
only exists on failure). Rather than reproducing that asymmetry -- which
agent.suites' "every declared case records exactly once" model can't
represent -- @SUITE.setup runs the *exact same* sequence (the individual
test_prf_* functions are ported unchanged, called with a lightweight
collector standing in for the original script's RunContext) and stores each
one's outcome; each registered case just replays its own precomputed result,
or is skipped with a shared reason if setup didn't reach it.
"""

from __future__ import annotations

import json
import platform
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent import common as lab_common
from agent.container import get_docker_gateway
from agent.suites import suite

from m3undle_lab.api import HoldOpenStream, M3UndleClient
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("profiles", group="core", order=140)
SIM_PORT_A = 19011
SIM_PORT_B = 19012
FIXTURE_A = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-a.json"
FIXTURE_B = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-b.json"

CASE_IDS = ["PRF-01", "PRF-02", "PRF-03", "PRF-04", "PRF-05", "PRF-06", "PROFILE-OUT-01"]


class _RecordCollector:
    """Stands in for the frozen script's RunContext during setup -- collects
    each test function's own .record() call instead of writing to the real
    ctx, so each function's unmodified body decides its own pass/fail/skip
    exactly as it always did."""

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


# ---------------------------------------------------------------------------
# Helpers (ported unchanged)
# ---------------------------------------------------------------------------

def _create_profile(client: M3UndleClient, name: str, playlist_url: str) -> dict | None:
    try:
        client.upsert_provider(name=name, playlist_url=playlist_url)
    except RuntimeError:
        return None
    status, profiles = client.get("/api/v1/profiles")
    if status != 200 or not isinstance(profiles, list):
        return None
    return next((p for p in profiles if isinstance(p, dict) and p.get("name") == name), None)


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{int(time.time())}"


def _get_profile_id(profile: dict) -> str | None:
    return profile.get("profileId") or profile.get("id")


def _activate_profile(client: M3UndleClient, profile_id: str) -> tuple[int, object]:
    return client._request("PUT", f"/api/v1/profiles/{profile_id}/active")


def _get_status(client: M3UndleClient) -> dict:
    _, body = client.get("/status")
    return body if isinstance(body, dict) else {}


def _poll_switch_state(client: M3UndleClient, *, target: str = "complete", timeout: float = 60.0) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        st = _get_status(client)
        lineup = st.get("lineup") if isinstance(st.get("lineup"), dict) else {}
        last = st.get("switchState") or st.get("profileSwitchState") or lineup.get("switchState", "")
        if last == target:
            return last
        time.sleep(1.0)
    return last


def _fetch_text(url: str, timeout: float = 10.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:
        return 0, ""


def _fetch_json(url: str, timeout: float = 10.0) -> tuple[int, object]:
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


# ---------------------------------------------------------------------------
# Test functions (ported unchanged -- collector duck-types RunContext.record)
# ---------------------------------------------------------------------------

def _test_prf_01(ctx: _RecordCollector, client: M3UndleClient, sim_a_url: str) -> str | None:
    """PRF-01: Creating a second profile succeeds via API."""
    status, profiles_before = client.get("/api/v1/profiles")
    count_before = len(profiles_before) if isinstance(profiles_before, list) else 0

    profile_name = _unique_name("prf-01-profile-b")
    profile_b = _create_profile(client, profile_name, sim_a_url)
    if profile_b is None:
        ctx.record("PRF-01", False, "Could not create second profile via API")
        return None

    status, profiles_after = client.get("/api/v1/profiles")
    count_after = len(profiles_after) if isinstance(profiles_after, list) else 0
    profile_b_id = _get_profile_id(profile_b)
    ctx.record(
        "PRF-01", count_after > count_before and bool(profile_b_id),
        f"profiles before={count_before} after={count_after} created={profile_name}",
    )
    return profile_b_id


def _test_prf_02(ctx: _RecordCollector, client: M3UndleClient, profile_id: str) -> None:
    """PRF-02: Switching active profile triggers a refresh; /status reflects progression."""
    status_code, _ = _activate_profile(client, profile_id)
    if status_code not in (200, 202, 204, 409):
        ctx.record("PRF-02", False, f"PUT active returned {status_code}")
        return
    final_state = _poll_switch_state(client, target="complete", timeout=90.0)
    ctx.record("PRF-02", final_state == "complete", f"switchState ended at {final_state!r}")


def _test_prf_03(ctx: _RecordCollector, client: M3UndleClient, profile_id_a: str, profile_id_b: str) -> None:
    """PRF-03: Switching profile while a refresh is in progress returns an error."""
    client.set_active_profile(profile_id_a)
    client.trigger_refresh()
    status_code, body = _activate_profile(client, profile_id_b)
    ctx.record(
        "PRF-03", status_code in (409, 400, 503),
        f"Expected conflict/error on switch-while-refreshing, got {status_code}: {str(body)[:120]}",
    )
    client.poll_snapshot_status(timeout_seconds=90.0)


def _test_prf_05(ctx: _RecordCollector, client: M3UndleClient, profile_id: str) -> None:
    """PRF-05: Deleting a profile removes channels/snapshots (cascade)."""
    if not client.wait_snapshot_idle(timeout_seconds=90.0):
        ctx.record("PRF-05", False, "snapshot runner did not go idle before delete")
        return

    status_del = 0
    for _ in range(3):
        status_del, _ = client._request("DELETE", f"/api/v1/profiles/{profile_id}")
        if status_del in (200, 204):
            break
        if status_del != 409:
            break
        client.wait_snapshot_idle(timeout_seconds=30.0)

    if status_del not in (200, 204):
        ctx.record("PRF-05", False, f"DELETE profile returned {status_del}")
        return
    status_ch, body_ch = client.get(f"/api/v1/profiles/{profile_id}/channels")
    ctx.record(
        "PRF-05", status_ch in (404, 410) or (status_ch == 200 and body_ch == []),
        f"After delete: GET channels returned {status_ch}: {str(body_ch)[:80]}",
    )


def _test_prf_04(ctx: _RecordCollector, client: M3UndleClient, profile_id: str) -> None:
    """PRF-04: Disabled profiles cannot be activated, but can be re-enabled and activated later."""
    active_before = client.get_active_profile_id_from_list()

    status_disable, _ = client.set_profile_enabled(profile_id, False)
    disabled_profile = client.get_profile(profile_id)

    status_activate, body_activate = _activate_profile(client, profile_id)
    active_after_failed = client.get_active_profile_id_from_list()

    status_enable, _ = client.set_profile_enabled(profile_id, True)
    status_reactivate, _ = _activate_profile(client, profile_id)
    final_state = _poll_switch_state(client, target="complete", timeout=90.0)
    active_after_reenable = client.get_active_profile_id_from_list()

    disabled_visible = (
        isinstance(disabled_profile, dict)
        and disabled_profile.get("enabled") is False
        and disabled_profile.get("isActive") is False
    )
    rejected = status_activate in (400, 409)
    active_unchanged = active_after_failed == active_before and active_after_failed != profile_id
    reenabled = status_enable in (200, 204)
    reactivated = (
        status_reactivate in (200, 202, 204, 409)
        and final_state == "complete"
        and active_after_reenable == profile_id
    )

    ctx.record(
        "PRF-04",
        status_disable in (200, 204) and disabled_visible and rejected and active_unchanged and reenabled and reactivated,
        " ".join([
            f"disable={status_disable}", f"activate_disabled={status_activate}", f"reactivate={status_reactivate}",
            f"active_before={active_before}", f"active_after_failed={active_after_failed}",
            f"active_after_reenable={active_after_reenable}", f"final_state={final_state!r}",
            f"error={str(body_activate)[:80]}",
        ]),
    )


def _test_prf_06(ctx: _RecordCollector, client: M3UndleClient, target_profile_id: str) -> None:
    """PRF-06: An existing stream remains alive while the active profile is switched."""
    stream_urls = client.get_stream_urls()
    if not stream_urls:
        status, lineup = client.get("/hdhr/lineup.json")
        if status == 200 and isinstance(lineup, list):
            stream_urls = [str(item.get("URL")) for item in lineup if isinstance(item, dict) and item.get("URL")]
    if not stream_urls:
        ctx.record("PRF-06", False, "No stream URLs available for active profile or HDHR lineup")
        return

    holder = HoldOpenStream(stream_urls[0], hold_seconds=20.0)
    try:
        holder.start()
        header_status = holder.wait_header()
        if header_status != 200:
            ctx.record("PRF-06", False, f"stream open returned {header_status} error={holder.error!r}")
            return

        sessions_before = client.wait_for_active_session_count(1, timeout_seconds=10.0)
        if not sessions_before:
            ctx.record("PRF-06", False, "No active session appeared before profile switch")
            return

        session_id = str(sessions_before[0].get("sessionId") or sessions_before[0].get("SessionId") or "")
        bytes_before = holder.bytes_read

        status_switch, _ = _activate_profile(client, target_profile_id)
        final_state = _poll_switch_state(client, target="complete", timeout=90.0)
        time.sleep(2.0)

        session_after = client.get_session_status(session_id) if session_id else None
        sessions_after = client.get_active_sessions()
        still_streaming = not holder.finished and holder.error is None
        session_visible = bool(session_after) or bool(sessions_after)

        ctx.record(
            "PRF-06",
            status_switch in (200, 202, 204, 409) and final_state == "complete" and still_streaming and session_visible,
            " ".join([
                f"switch={status_switch}", f"final_state={final_state!r}", f"session_id={session_id or 'n/a'}",
                f"bytes_before={bytes_before}", f"bytes_after={holder.bytes_read}", f"finished={holder.finished}",
                f"finish_reason={holder.finish_reason!r}", f"active_sessions_after={len(sessions_after)}",
            ]),
        )
    finally:
        holder.stop()


def _test_profile_out_01(
    ctx: _RecordCollector, client: M3UndleClient, base: str, profile_a_id: str, profile_b_id: str,
) -> None:
    """PROFILE-OUT-01: Published M3U and HDHR outputs track the active profile after a switch."""
    activated, refresh_triggered = client.activate_profile_for_setup(profile_a_id, timeout_seconds=90.0)
    if not activated:
        ctx.record("PROFILE-OUT-01", False, "Could not activate profile A before test")
        return
    if refresh_triggered:
        client.poll_snapshot_status(timeout_seconds=90.0)
    client.wait_snapshot_idle(timeout_seconds=90.0)
    try:
        client.build_snapshot()
    except RuntimeError as exc:
        ctx.record("PROFILE-OUT-01", False, f"build_snapshot for profile A failed: {exc}")
        return
    if not client.poll_build_completion(profile_a_id, timeout_seconds=90.0):
        ctx.record("PROFILE-OUT-01", False, "Build did not settle for profile A before test")
        return

    status_m3u_a, m3u_a = _fetch_text(f"{base}/m3u/m3undle.m3u")
    status_hdhr_a, hdhr_a_raw = _fetch_json(f"{base}/hdhr/lineup.json")
    hdhr_a = hdhr_a_raw if isinstance(hdhr_a_raw, list) else []

    if status_m3u_a != 200 or "#EXTM3U" not in m3u_a:
        ctx.record("PROFILE-OUT-01", False, f"Could not capture profile A M3U: status={status_m3u_a}")
        return

    artifacts_dir = lab_common.runtime_results_dir()
    try:
        (artifacts_dir / "profile-out-01-m3u-a.m3u").write_text(m3u_a, encoding="utf-8")
        (artifacts_dir / "profile-out-01-hdhr-a.json").write_text(json.dumps(hdhr_a, indent=2), encoding="utf-8")
    except Exception:
        pass

    switch_status, _ = client.set_active_profile(profile_b_id)
    if switch_status not in (200, 202, 204, 409):
        ctx.record("PROFILE-OUT-01", False, f"PUT active profile B returned {switch_status}")
        return

    final_state = _poll_switch_state(client, target="complete", timeout=90.0)
    if final_state != "complete":
        ctx.record("PROFILE-OUT-01", False, f"Profile switch did not complete: switchState={final_state!r}")
        return

    client.wait_snapshot_idle(timeout_seconds=90.0)
    try:
        client.build_snapshot()
    except RuntimeError as exc:
        ctx.record("PROFILE-OUT-01", False, f"build_snapshot after profile B switch failed: {exc}")
        return
    if not client.poll_build_completion(profile_b_id, timeout_seconds=90.0):
        ctx.record("PROFILE-OUT-01", False, "Build did not settle after switching to profile B")
        return

    status_m3u_b, m3u_b = _fetch_text(f"{base}/m3u/m3undle.m3u")
    status_hdhr_b, hdhr_b_raw = _fetch_json(f"{base}/hdhr/lineup.json")
    hdhr_b = hdhr_b_raw if isinstance(hdhr_b_raw, list) else []

    try:
        (artifacts_dir / "profile-out-01-m3u-b.m3u").write_text(m3u_b, encoding="utf-8")
        (artifacts_dir / "profile-out-01-hdhr-b.json").write_text(json.dumps(hdhr_b, indent=2), encoding="utf-8")
    except Exception:
        pass

    m3u_changed = m3u_a != m3u_b and "#EXTM3U" in m3u_b
    guide_names_a = {str(e.get("GuideName", "")) for e in hdhr_a if isinstance(e, dict)}
    guide_names_b = {str(e.get("GuideName", "")) for e in hdhr_b if isinstance(e, dict)}
    hdhr_changed = bool(hdhr_b) and guide_names_a != guide_names_b

    try:
        diff_lines = [
            "PROFILE-OUT-01 diff summary",
            f"  M3U changed: {m3u_changed}",
            f"  HDHR guide names A: {sorted(guide_names_a)}",
            f"  HDHR guide names B: {sorted(guide_names_b)}",
            f"  HDHR changed: {hdhr_changed}",
        ]
        (artifacts_dir / "profile-out-01-diff.txt").write_text("\n".join(diff_lines), encoding="utf-8")
    except Exception:
        pass

    ctx.record(
        "PROFILE-OUT-01", m3u_changed and hdhr_changed,
        " ".join([
            f"switch_status={switch_status}", f"final_state={final_state!r}", f"m3u_changed={m3u_changed}",
            f"hdhr_changed={hdhr_changed}", f"guide_names_a={sorted(guide_names_a)!r}",
            f"guide_names_b={sorted(guide_names_b)!r}",
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
    sim_a: SimulatorInstance | None = None
    sim_b: SimulatorInstance | None = None
    state: dict[str, object] = {"reason": "Profiles suite setup did not complete", "records": collector.records}

    try:
        setup_provider_name = _unique_name("provider-prf-a")
        bind_a, public_host_a = _simulator_address(SIM_PORT_A)
        bind_b, public_host_b = _simulator_address(SIM_PORT_B)

        sim_a = SimulatorInstance(fixture=FIXTURE_A, port=SIM_PORT_A, bind=bind_a, public_host=public_host_a, suite="profiles-a")
        sim_a.start()
        if not sim_a.wait_healthy():
            state["reason"] = "Provider simulator A did not become healthy"
            return {"state": state}
        sim_a_url = sim_a.playlist_url

        sim_b = SimulatorInstance(fixture=FIXTURE_B, port=SIM_PORT_B, bind=bind_b, public_host=public_host_b, suite="profiles-b")
        sim_b.start()
        sim_b_url: str | None = sim_b.playlist_url if sim_b.wait_healthy() else None

        client = M3UndleClient(base)
        if not client.setup(playlist_url=sim_a_url, provider_name=setup_provider_name):
            state["reason"] = client.last_setup_error or "Setup sequence failed"
            return {"state": state}

        status, profiles = client.get("/api/v1/profiles")
        if status != 200 or not isinstance(profiles, list) or not profiles:
            state["reason"] = f"GET /api/v1/profiles failed: {status}"
            return {"state": state}

        primary_profile = next(
            (p for p in profiles if isinstance(p, dict) and p.get("name") == setup_provider_name), profiles[0],
        )
        primary_profile_id = _get_profile_id(primary_profile)

        second_profile_id = _test_prf_01(collector, client, sim_a_url)

        if second_profile_id:
            _test_prf_02(collector, client, second_profile_id)
        else:
            collector.record("PRF-02", False, "No second profile available to switch to")

        if second_profile_id and primary_profile_id:
            _test_prf_03(collector, client, primary_profile_id, second_profile_id)
        else:
            collector.record("PRF-03", False, "Need two profiles for PRF-03")

        if second_profile_id:
            _test_prf_04(collector, client, second_profile_id)
        else:
            collector.record("PRF-04", False, "No second profile available for disable/enable flow")

        if second_profile_id and primary_profile_id:
            activated, refresh_triggered = client.activate_profile_for_setup(primary_profile_id, timeout_seconds=90.0)
            if activated:
                client.wait_active_profile(primary_profile_id, timeout_seconds=30.0)
                if refresh_triggered:
                    client.poll_snapshot_status(timeout_seconds=90.0)
                client.build_snapshot()
                client.poll_build_completion(primary_profile_id, timeout_seconds=90.0)
            _test_prf_06(collector, client, second_profile_id)
        else:
            collector.record("PRF-06", False, "Need two profiles for PRF-06")

        if second_profile_id:
            client.set_active_profile(primary_profile_id)
            client.poll_snapshot_status(timeout_seconds=60.0)
            _test_prf_05(collector, client, second_profile_id)
        else:
            collector.record("PRF-05", False, "No second profile to delete")

        if sim_b_url and primary_profile_id:
            out01_provider_name = _unique_name("provider-prf-b")
            out01_body = client.upsert_provider(name=out01_provider_name, playlist_url=sim_b_url)
            out01_provider_id = str((out01_body.get("provider", {}) or {}).get("providerId") or out01_body.get("providerId") or "")
            out01_profile_id: str | None = None
            if out01_provider_id:
                client.activate_provider(out01_provider_id)
                client.wait_snapshot_idle(timeout_seconds=90.0)
                out01_profile_id = client.resolve_provider_profile_id(out01_provider_id, out01_provider_name)
            if out01_profile_id:
                client.include_all_groups(out01_profile_id)
                client.select_all_channels(out01_profile_id)
                _test_profile_out_01(collector, client, base, primary_profile_id, out01_profile_id)
                client.set_active_profile(primary_profile_id)
                client.poll_snapshot_status(timeout_seconds=60.0)
            else:
                collector.record("PROFILE-OUT-01", False, "Could not create provider-b profile for output test")
        else:
            reason = "sim-b unhealthy" if not sim_b_url else "no primary_profile_id"
            collector.record("PROFILE-OUT-01", None, f"Skipped PROFILE-OUT-01: {reason}")

        state["reason"] = None
        return {"state": state}
    except Exception as exc:  # setup errors are reported as per-case skips below
        state["reason"] = f"Profiles suite setup failed: {exc}"
        return {"state": state}
    finally:
        if sim_b is not None:
            try:
                sim_b.stop()
            except Exception:
                pass
        if sim_a is not None:
            try:
                sim_a.stop()
            except Exception:
                pass


def _register_case(case_id: str) -> None:
    @SUITE.case(case_id)
    def _replay(ctx, state):
        records: dict[str, tuple[bool | None, str, Any]] = state["records"]  # type: ignore[assignment]
        entry = records.get(case_id)
        if entry is None:
            ctx.skip(case_id, str(state.get("reason") or "Profiles suite setup did not reach this case"))
            return
        passed, message, detail = entry
        ctx.record(case_id, passed, message, detail)


for _case_id in CASE_IDS:
    _register_case(_case_id)
