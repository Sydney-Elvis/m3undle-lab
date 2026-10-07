"""Lineup protection: a provider outage, URL change, or bad response must never cost mapped channels.

Replays the production incident against the provider simulator: an IPTV panel changed its URL, the
old URL stopped working for days, and the first successful refresh on the new URL wiped every mapped
channel. These cases pin the behaviour that prevents it:

  * channel identity follows the stream, not the provider URL (host/port/credentials can change)
  * a failed fetch changes nothing
  * a fetch that "succeeds" but returns a fraction of the lineup is held, not applied
  * a held fetch is accepted only when the provider keeps returning the same smaller lineup
  * mapped channels come back with the same ids, states, numbers and overrides when the provider does

The cases share one provider and run in order; each later case builds on the state the previous one
left behind, so a failure in an early case skips the cases that depend on it.

Cases (all against an M3U provider whose playlist carries Xtream-shaped stream URLs, so no encryption
key is needed; LINEUP-09 repeats the host change against a native Xtream provider and skips when the
instance has no M3UNDLE_ENCRYPTION_KEY):

  LINEUP-01  old host goes away: failed refreshes change nothing
  LINEUP-02  provider moves to a new host: same channel ids, mapping untouched
  LINEUP-03  fetch returns 1 of 24 channels: held, last lineup kept, reported as unhealthy
  LINEUP-04  provider recovers: lineup and mapping identical to the start
  LINEUP-05  moderate change (24 -> 18) applies normally, then restores
  LINEUP-06  persistent shrink (24 -> 8): held twice, accepted on the third matching refresh
  LINEUP-07  provider restores the full lineup: every mapped channel returns with its mapping
  LINEUP-08  empty-ish provider response never wipes the lineup (single channel, then recovery)
  LINEUP-09  native Xtream provider moves host: mapping survives
"""

from __future__ import annotations

import json
import platform
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent.container import get_docker_gateway
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("lineup-protection", group="core", order=190)

SIM_PORT_OLD = 19040
SIM_PORT_NEW = 19041
SIM_PORT_XTREAM_OLD = 19042
SIM_PORT_XTREAM_NEW = 19043

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
BASE_FIXTURE = FIXTURES_DIR / "provider-xtream.json"
# The docker simulator backend only mounts the lab fixtures directory, so generated fixtures live there.
GENERATED_DIR = FIXTURES_DIR / "generated"
FIXTURE_PATH = GENERATED_DIR / "lineup-protection.json"

FULL_LINEUP = 24
XTREAM_USER = "xtreamuser"
XTREAM_PASS = "xtreampass"
HELD_HINT = "last known lineup was kept"


# ---------------------------------------------------------------------------
# Simulator helpers
# ---------------------------------------------------------------------------

def _simulator_address(port: int) -> tuple[str, str | None]:
    """Return the listener and advertised URL the M3Undle container can reach."""
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{port}"
    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{port}"
    return "127.0.0.1", None


def _write_fixture(count: int) -> None:
    """Write a provider fixture with `count` live channels (stable ids, so a smaller one is a strict subset)."""
    base = json.loads(BASE_FIXTURE.read_text(encoding="utf-8"))
    channels: dict[str, Any] = {}
    stream_ids: dict[str, int] = {}
    for n in range(1, count + 1):
        channel_id = f"lp-channel-{n:03d}"
        channels[channel_id] = {
            "display_name": f"LP Channel {n:02d}",
            "group_title": "LP-Sports" if n % 2 else "LP-News",
            "tvg_id": f"lp-{n:03d}",
            "payload": f"lp-payload-{n:03d}",
        }
        stream_ids[channel_id] = 70000 + n
    fixture = {
        "provider_name": "provider-lineup-protection",
        "max_streams": 4,
        "defaults": base["defaults"],
        "channels": channels,
        "xtream": {
            "username": XTREAM_USER,
            "password": XTREAM_PASS,
            "categories": [
                {"category_id": "10", "category_name": "LP-Sports", "parent_id": 0},
                {"category_id": "11", "category_name": "LP-News", "parent_id": 0},
            ],
            "stream_ids": stream_ids,
            "vod_categories": [],
            "vod_streams": [],
            "series_categories": [],
            "series": [],
            "series_info": {},
        },
    }
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps(fixture, indent=2), encoding="utf-8")


def _start_simulator(port: int) -> SimulatorInstance:
    bind, public_host = _simulator_address(port)
    simulator = SimulatorInstance(
        fixture=FIXTURE_PATH, port=port, bind=bind, public_host=public_host, suite="lineup-protection",
    )
    simulator.start()
    if not simulator.wait_healthy():
        raise RuntimeError(f"Provider simulator on port {port} did not become healthy")
    return simulator


def _reload_fixture(port: int, count: int) -> None:
    """Change what a running simulator serves: rewrite the fixture, then have the engine re-read it."""
    _write_fixture(count)
    request = urllib.request.Request(f"http://127.0.0.1:{port}/debug/reload", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10.0) as response:
        if response.status != 200:
            raise RuntimeError(f"/debug/reload returned {response.status}")


def _playlist_url(simulator: SimulatorInstance) -> str:
    return f"{simulator.public_host}/get.php?username={XTREAM_USER}&password={XTREAM_PASS}&type=m3u_plus"


# ---------------------------------------------------------------------------
# M3Undle observation helpers
# ---------------------------------------------------------------------------

def _refresh(client: M3UndleClient, timeout_seconds: float = 120.0) -> dict[str, Any]:
    """Trigger a refresh and return the snapshot status of the run it produced (ok, fail, suspect, ...)."""
    previous = str(client.get_snapshot_status_info().get("startedUtc") or "")
    client.trigger_refresh()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        info = client.get_snapshot_status_info()
        started = str(info.get("startedUtc") or "")
        if started and started != previous and not info.get("running"):
            return info
        time.sleep(1.0)
    raise RuntimeError("Refresh did not finish within the timeout")


def _published_names(client: M3UndleClient) -> set[str]:
    """Channel names clients actually see in the published M3U."""
    status, body = client.get("/m3u/m3undle.m3u")
    if status != 200 or not isinstance(body, str):
        raise RuntimeError(f"Published M3U returned status={status}")
    names = set()
    for line in body.splitlines():
        if line.startswith("#EXTINF") and "," in line:
            names.add(line.rsplit(",", 1)[1].strip())
    return names


def _selections(client: M3UndleClient, profile_id: str) -> dict[str, tuple[Any, ...]]:
    """providerChannelId -> mapping (state, number, override) across every group of the profile."""
    status, filters = client.get(f"/api/v1/profiles/{profile_id}/group-filters")
    if status != 200 or not isinstance(filters, list):
        raise RuntimeError(f"group-filters returned status={status}")
    result: dict[str, tuple[Any, ...]] = {}
    for item in filters:
        filter_id = str(item["profileGroupFilterId"])
        sel_status, body = client.get(f"/api/v1/profiles/{profile_id}/group-filters/{filter_id}/channel-selections")
        if sel_status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"channel-selections returned status={sel_status} for {filter_id}")
        for channel in body.get("channels", []):
            result[str(channel["providerChannelId"])] = (
                channel.get("state"),
                channel.get("channelNumber"),
                channel.get("displayNameOverride"),
                channel.get("outputGroupName"),
            )
    return result


def _configure_mapping(client: M3UndleClient, profile_id: str) -> None:
    """Give the profile real per-channel intent: numbers, a rename, and exclusions."""
    status, filters = client.get(f"/api/v1/profiles/{profile_id}/group-filters")
    if status != 200 or not isinstance(filters, list) or len(filters) != 2:
        raise RuntimeError(f"Expected two provider groups, got status={status} body={filters}")
    number = 700
    for item in filters:
        filter_id = str(item["profileGroupFilterId"])
        raw_name = str(item["providerGroupRawName"])
        patch_status, _ = client._request(
            "PATCH", f"/api/v1/profiles/{profile_id}/group-filters/{filter_id}",
            body={"decision": "include", "channelMode": "select"},
        )
        if patch_status != 200:
            raise RuntimeError(f"Group update returned {patch_status} for {raw_name}")
        sel_status, selections = client.get(f"/api/v1/profiles/{profile_id}/group-filters/{filter_id}/channel-selections")
        if sel_status != 200 or not isinstance(selections, dict):
            raise RuntimeError(f"Selection read returned {sel_status} for {raw_name}")
        channels = [c for c in selections.get("channels", []) if isinstance(c, dict)]
        requested = []
        for index, channel in enumerate(channels):
            entry: dict[str, Any] = {"providerChannelId": channel["providerChannelId"], "state": "included"}
            if index < 3:
                entry["channelNumber"] = number
                number += 1
            if raw_name == "LP-Sports" and index == 0:
                entry["displayNameOverride"] = "LP Renamed"
            if index == len(channels) - 1:
                entry["state"] = "excluded"
            requested.append(entry)
        put_status, put_body = client._request(
            "PUT", f"/api/v1/profiles/{profile_id}/group-filters/{filter_id}/channel-selections",
            body={"channelMode": "select", "channels": requested},
        )
        if put_status != 200:
            raise RuntimeError(f"Selection update returned {put_status} for {raw_name}: {put_body}")
    client.build_snapshot()
    if not client.poll_build_completion(profile_id):
        raise RuntimeError("Configured snapshot build did not complete")


def _update_provider_url(client: M3UndleClient, provider_id: str, profile_id: str, name: str, playlist_url: str) -> None:
    status, body = client._request(
        "PUT", f"/api/v1/providers/{provider_id}",
        body={
            "name": name,
            "playlistUrl": playlist_url,
            "enabled": True,
            "includeVod": False,
            "includeSeries": False,
            "timeoutSeconds": 120,
            "maxConcurrentStreams": 2,
            "associateToProfileIds": [profile_id],
        },
    )
    if status != 200:
        raise RuntimeError(f"Provider URL update returned {status}: {body}")
    client.wait_snapshot_idle(timeout_seconds=60.0)


def _provider_health(client: M3UndleClient, provider_id: str) -> dict[str, Any]:
    status, body = client.get(f"/api/v1/providers/{provider_id}/health")
    return body if status == 200 and isinstance(body, dict) else {}


# ---------------------------------------------------------------------------
# Suite plumbing
# ---------------------------------------------------------------------------

@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    state: dict[str, object] = {
        "ready": False,
        "reason": "Lineup protection suite setup did not complete",
        "client": None,
        "sim_old": None,
        "sim_new": None,
        "sims_xtream": [],
        "provider_name": f"lineup-protection-{int(time.time())}",
        "completed": set(),
    }
    try:
        _write_fixture(FULL_LINEUP)
        sim_old = _start_simulator(SIM_PORT_OLD)
        state["sim_old"] = sim_old

        client = M3UndleClient(base_url)
        state["client"] = client
        if not client.setup(playlist_url=_playlist_url(sim_old), provider_name=str(state["provider_name"])):
            state["reason"] = client.last_setup_error or "M3Undle provider setup failed"
            return {"state": state}

        profile_id = str(client.profile_id or "")
        provider_id = str(client.provider_id or "")
        if not profile_id or not provider_id:
            state["reason"] = "No profile or provider after setup"
            return {"state": state}

        _configure_mapping(client, profile_id)

        baseline_names = _published_names(client)
        baseline_selections = _selections(client, profile_id)
        if len(baseline_selections) != FULL_LINEUP:
            state["reason"] = f"Expected {FULL_LINEUP} provider channels after setup, saw {len(baseline_selections)}"
            return {"state": state}
        if not baseline_names or "LP Renamed" not in baseline_names:
            state["reason"] = f"Published lineup is missing the mapped channels: {sorted(baseline_names)[:5]}"
            return {"state": state}

        state.update(
            profile_id=profile_id,
            provider_id=provider_id,
            baseline_names=baseline_names,
            baseline_selections=baseline_selections,
            ready=True,
            reason=None,
        )
        return {"state": state}
    except Exception as exc:  # setup errors are reported as a case skip below
        state["reason"] = f"Lineup protection suite setup failed: {exc}"
        return {"state": state}


def _ready(ctx: Any, state: dict[str, object], case_id: str, *, needs: str | None = None) -> M3UndleClient | None:
    client = state.get("client")
    if not state.get("ready") or not isinstance(client, M3UndleClient):
        ctx.skip(case_id, str(state.get("reason") or "Lineup protection suite setup did not complete"))
        return None
    if needs is not None and needs not in state["completed"]:  # type: ignore[operator]
        ctx.skip(case_id, f"{needs} did not pass, so this case has no valid starting state")
        return None
    return client


def _finish(ctx: Any, state: dict[str, object], case_id: str, reasons: list[str], ok_message: str) -> None:
    if reasons:
        ctx.record(case_id, False, "; ".join(reasons))
        return
    state["completed"].add(case_id)  # type: ignore[union-attr]
    ctx.record(case_id, True, ok_message)


def _expect_unchanged(client: M3UndleClient, state: dict[str, object], reasons: list[str], when: str) -> None:
    """The invariant: published lineup and every mapping exactly as they were at the start."""
    names = _published_names(client)
    baseline_names = state["baseline_names"]
    if names != baseline_names:
        reasons.append(
            f"{when}: published lineup changed (missing={sorted(baseline_names - names)[:4]} "  # type: ignore[operator]
            f"extra={sorted(names - baseline_names)[:4]})"  # type: ignore[operator]
        )
    selections = _selections(client, str(state["profile_id"]))
    baseline_selections: dict[str, tuple[Any, ...]] = state["baseline_selections"]  # type: ignore[assignment]
    if selections != baseline_selections:
        lost = sorted(set(baseline_selections) - set(selections))
        added = sorted(set(selections) - set(baseline_selections))
        changed = sorted(k for k in set(selections) & set(baseline_selections) if selections[k] != baseline_selections[k])
        reasons.append(f"{when}: mapping changed (lost={len(lost)} added={len(added)} altered={len(changed)})")


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

@SUITE.case("LINEUP-01")
def lineup_01(ctx: Any, state: dict[str, object]) -> None:
    """The old host stops working (the incident's multi-day outage): every refresh fails, nothing changes."""
    client = _ready(ctx, state, "LINEUP-01")
    if client is None:
        return
    reasons: list[str] = []
    try:
        sim_old = state["sim_old"]
        assert isinstance(sim_old, SimulatorInstance)
        sim_old.stop()
        statuses = []
        for _ in range(3):
            statuses.append(str(_refresh(client).get("lastStatus")))
        if any(s != "fail" for s in statuses):
            reasons.append(f"refreshes against a dead host should fail, got {statuses}")
        _expect_unchanged(client, state, reasons, "after failed refreshes")
        health = _provider_health(client, str(state["provider_id"]))
        if health.get("status") == "healthy":
            reasons.append("provider still reported healthy after failed refreshes")
    except Exception as exc:
        reasons.append(f"unexpected error: {exc}")
    _finish(ctx, state, "LINEUP-01", reasons, "3 failed refreshes left the lineup and every mapping untouched")


@SUITE.case("LINEUP-02")
def lineup_02(ctx: Any, state: dict[str, object]) -> None:
    """The provider reappears on a new host: channels keep their ids, so the mapping is untouched."""
    client = _ready(ctx, state, "LINEUP-02", needs="LINEUP-01")
    if client is None:
        return
    reasons: list[str] = []
    try:
        _write_fixture(FULL_LINEUP)
        sim_new = _start_simulator(SIM_PORT_NEW)
        state["sim_new"] = sim_new
        _update_provider_url(client, str(state["provider_id"]), str(state["profile_id"]),
                             str(state["provider_name"]), _playlist_url(sim_new))
        info = _refresh(client)
        if info.get("lastStatus") != "ok":
            reasons.append(f"refresh on the new host ended {info.get('lastStatus')!r}: {info.get('errorSummary')}")
        _expect_unchanged(client, state, reasons, "after the host change")
    except Exception as exc:
        reasons.append(f"unexpected error: {exc}")
    _finish(ctx, state, "LINEUP-02", reasons, "new host adopted every existing channel; lineup and mapping identical")


@SUITE.case("LINEUP-03")
def lineup_03(ctx: Any, state: dict[str, object]) -> None:
    """A fetch returns 1 of 24 channels: held, nothing written, reported."""
    client = _ready(ctx, state, "LINEUP-03", needs="LINEUP-02")
    if client is None:
        return
    reasons: list[str] = []
    try:
        _reload_fixture(SIM_PORT_NEW, 1)
        info = _refresh(client)
        if info.get("lastStatus") != "suspect":
            reasons.append(f"a 1-of-{FULL_LINEUP} fetch should be held as 'suspect', got {info.get('lastStatus')!r}")
        if HELD_HINT not in str(info.get("errorSummary") or ""):
            reasons.append(f"held fetch should say the last lineup was kept, got {info.get('errorSummary')!r}")
        _expect_unchanged(client, state, reasons, "while the fetch is held")
        health = _provider_health(client, str(state["provider_id"]))
        if health.get("status") == "healthy":
            reasons.append("a held fetch must not leave the provider reported healthy")
        if HELD_HINT not in str(health.get("lastError") or ""):
            reasons.append(f"provider health should carry the held-fetch reason, got {health.get('lastError')!r}")
    except Exception as exc:
        reasons.append(f"unexpected error: {exc}")
    _finish(ctx, state, "LINEUP-03", reasons, "1-of-24 fetch held; lineup, mapping and ids untouched; reported as unhealthy")


@SUITE.case("LINEUP-04")
def lineup_04(ctx: Any, state: dict[str, object]) -> None:
    """The provider recovers: it heals on its own, identical to the start."""
    client = _ready(ctx, state, "LINEUP-04", needs="LINEUP-03")
    if client is None:
        return
    reasons: list[str] = []
    try:
        _reload_fixture(SIM_PORT_NEW, FULL_LINEUP)
        info = _refresh(client)
        if info.get("lastStatus") != "ok":
            reasons.append(f"refresh after recovery ended {info.get('lastStatus')!r}: {info.get('errorSummary')}")
        _expect_unchanged(client, state, reasons, "after recovery")
        health = _provider_health(client, str(state["provider_id"]))
        if health.get("status") != "healthy":
            reasons.append(f"provider should be healthy again, reported {health.get('status')!r}")
    except Exception as exc:
        reasons.append(f"unexpected error: {exc}")
    _finish(ctx, state, "LINEUP-04", reasons, "recovered without intervention; lineup and mapping identical to the start")


@SUITE.case("LINEUP-05")
def lineup_05(ctx: Any, state: dict[str, object]) -> None:
    """24 -> 18 is an ordinary change: applied at once (not held), and fully reversible."""
    client = _ready(ctx, state, "LINEUP-05", needs="LINEUP-04")
    if client is None:
        return
    reasons: list[str] = []
    try:
        _reload_fixture(SIM_PORT_NEW, 18)
        info = _refresh(client)
        if info.get("lastStatus") != "ok":
            reasons.append(f"a 24 -> 18 change should apply normally, got {info.get('lastStatus')!r}: {info.get('errorSummary')}")
        names = _published_names(client)
        removed = {f"LP Channel {n:02d}" for n in range(19, FULL_LINEUP + 1)}
        leaked = sorted(names & removed)
        if leaked:
            reasons.append(f"channels the provider dropped are still published: {leaked[:4]}")
        baseline_names: set[str] = state["baseline_names"]  # type: ignore[assignment]
        missing = sorted(n for n in baseline_names if n not in removed and n not in names)
        if missing:
            reasons.append(f"channels the provider still lists went missing: {missing[:4]}")

        _reload_fixture(SIM_PORT_NEW, FULL_LINEUP)
        info = _refresh(client)
        if info.get("lastStatus") != "ok":
            reasons.append(f"restoring the lineup ended {info.get('lastStatus')!r}")
        _expect_unchanged(client, state, reasons, "after restoring 24 channels")
    except Exception as exc:
        reasons.append(f"unexpected error: {exc}")
    _finish(ctx, state, "LINEUP-05", reasons, "24 -> 18 applied immediately; restoring 24 returned the identical lineup and mapping")


@SUITE.case("LINEUP-06")
def lineup_06(ctx: Any, state: dict[str, object]) -> None:
    """24 -> 8 that persists: held twice, accepted on the third matching refresh."""
    client = _ready(ctx, state, "LINEUP-06", needs="LINEUP-05")
    if client is None:
        return
    reasons: list[str] = []
    try:
        _reload_fixture(SIM_PORT_NEW, 8)
        first = _refresh(client)
        second = _refresh(client)
        for label, info in (("first", first), ("second", second)):
            if info.get("lastStatus") != "suspect":
                reasons.append(f"{label} refresh of the shrunken lineup should be held, got {info.get('lastStatus')!r}")
        if not reasons:
            _expect_unchanged(client, state, reasons, "while the shrink is still held")

        third = _refresh(client)
        if third.get("lastStatus") != "ok":
            reasons.append(f"third matching refresh should be accepted, got {third.get('lastStatus')!r}: {third.get('errorSummary')}")
        names = _published_names(client)
        dropped = {f"LP Channel {n:02d}" for n in range(9, FULL_LINEUP + 1)}
        leaked = sorted(names & dropped)
        if leaked:
            reasons.append(f"accepted shrink still publishes dropped channels: {leaked[:4]}")
        if not names:
            reasons.append("accepted shrink published nothing")
    except Exception as exc:
        reasons.append(f"unexpected error: {exc}")
    _finish(ctx, state, "LINEUP-06", reasons, "shrink held for 2 refreshes, applied on the 3rd")


@SUITE.case("LINEUP-07")
def lineup_07(ctx: Any, state: dict[str, object]) -> None:
    """After a legitimate shrink, the provider restores the full lineup: mapped channels return with their mapping."""
    client = _ready(ctx, state, "LINEUP-07", needs="LINEUP-06")
    if client is None:
        return
    reasons: list[str] = []
    try:
        _reload_fixture(SIM_PORT_NEW, FULL_LINEUP)
        info = _refresh(client)
        if info.get("lastStatus") != "ok":
            reasons.append(f"refresh with the full lineup ended {info.get('lastStatus')!r}: {info.get('errorSummary')}")
        _expect_unchanged(client, state, reasons, "after the full lineup returned")
    except Exception as exc:
        reasons.append(f"unexpected error: {exc}")
    _finish(ctx, state, "LINEUP-07", reasons,
            "every channel removed by the shrink came back with the same id, state, number and rename")


@SUITE.case("LINEUP-08")
def lineup_08(ctx: Any, state: dict[str, object]) -> None:
    """Bad single-channel responses are held while the streak is short, a healthy fetch resets it, and the lineup heals."""
    client = _ready(ctx, state, "LINEUP-08", needs="LINEUP-07")
    if client is None:
        return
    reasons: list[str] = []
    try:
        _reload_fixture(SIM_PORT_NEW, 1)
        statuses = [str(_refresh(client).get("lastStatus")) for _ in range(2)]
        if statuses != ["suspect", "suspect"]:
            reasons.append(f"two bad responses in a row should both be held, got {statuses}")
        _expect_unchanged(client, state, reasons, "after two bad responses")

        # The provider fixes itself before the streak reaches the acceptance threshold.
        _reload_fixture(SIM_PORT_NEW, FULL_LINEUP)
        info = _refresh(client)
        if info.get("lastStatus") != "ok":
            reasons.append(f"refresh after the provider fixed itself ended {info.get('lastStatus')!r}")

        # A healthy fetch resets the streak: one more bad response is held again, not accepted.
        _reload_fixture(SIM_PORT_NEW, 1)
        info = _refresh(client)
        if info.get("lastStatus") != "suspect":
            reasons.append(f"a bad response after recovery should start a new streak, got {info.get('lastStatus')!r}")

        _reload_fixture(SIM_PORT_NEW, FULL_LINEUP)
        _refresh(client)
        _expect_unchanged(client, state, reasons, "after the provider recovered again")
    except Exception as exc:
        reasons.append(f"unexpected error: {exc}")
    _finish(ctx, state, "LINEUP-08", reasons, "bad responses never wiped the lineup; a healthy fetch reset the streak")


@SUITE.case("LINEUP-09")
def lineup_09(ctx: Any, state: dict[str, object]) -> None:
    """The incident exactly: a native Xtream provider moves to a new host. Skips without an encryption key."""
    client = state.get("client")
    if not isinstance(client, M3UndleClient) or not state.get("ready"):
        ctx.skip("LINEUP-09", str(state.get("reason") or "Lineup protection suite setup did not complete"))
        return

    sims: list[SimulatorInstance] = state["sims_xtream"]  # type: ignore[assignment]
    reasons: list[str] = []
    provider_name = f"lineup-protection-xtream-{int(time.time())}"
    try:
        _write_fixture(FULL_LINEUP)
        sim_old = _start_simulator(SIM_PORT_XTREAM_OLD)
        sims.append(sim_old)
        if not client.setup_xtream(
            xtream_base_url=sim_old.public_host, xtream_username=XTREAM_USER, xtream_password=XTREAM_PASS,
            provider_name=provider_name,
        ):
            error = client.last_setup_error or ""
            if "M3UNDLE_ENCRYPTION_KEY is not configured" in error:
                ctx.skip("LINEUP-09", "Xtream providers need M3UNDLE_ENCRYPTION_KEY in lab.env")
                return
            ctx.fail("LINEUP-09", f"Xtream provider setup failed: {error}")
            return

        provider_id = str(client.provider_id or "")
        profile_id = str(client.profile_id or "")
        _configure_mapping(client, profile_id)
        before_names = _published_names(client)
        before_selections = _selections(client, profile_id)

        sim_old.stop()
        sim_new = _start_simulator(SIM_PORT_XTREAM_NEW)
        sims.append(sim_new)
        status, body = client._request(
            "PUT", f"/api/v1/providers/{provider_id}",
            body={
                "name": provider_name,
                "xtreamBaseUrl": sim_new.public_host,
                "xtreamUsername": XTREAM_USER,
                "enabled": True,
                "includeVod": False,
                "includeSeries": False,
                "timeoutSeconds": 120,
                "maxConcurrentStreams": 2,
                "associateToProfileIds": [profile_id],
            },
        )
        if status != 200:
            ctx.fail("LINEUP-09", f"Xtream host update returned {status}: {body}")
            return
        client.wait_snapshot_idle(timeout_seconds=60.0)
        info = _refresh(client)
        if info.get("lastStatus") != "ok":
            reasons.append(f"refresh on the new Xtream host ended {info.get('lastStatus')!r}: {info.get('errorSummary')}")
        after_names = _published_names(client)
        after_selections = _selections(client, profile_id)
        if after_names != before_names:
            reasons.append(f"published lineup changed (missing={sorted(before_names - after_names)[:4]})")
        if after_selections != before_selections:
            reasons.append(
                f"mapping changed (lost={len(set(before_selections) - set(after_selections))} "
                f"added={len(set(after_selections) - set(before_selections))})"
            )
    except Exception as exc:
        reasons.append(f"unexpected error: {exc}")
    if reasons:
        ctx.record("LINEUP-09", False, "; ".join(reasons))
    else:
        ctx.record("LINEUP-09", True, "native Xtream provider moved host; every channel id and mapping survived")


@SUITE.teardown
def teardown(state: dict[str, object]) -> None:
    client = state.get("client")
    if isinstance(client, M3UndleClient):
        try:
            client.clear_existing_providers()
        except Exception:
            pass

    simulators = [state.get("sim_old"), state.get("sim_new"), *list(state.get("sims_xtream") or [])]  # type: ignore[arg-type]
    for simulator in simulators:
        if isinstance(simulator, SimulatorInstance):
            try:
                simulator.stop()
            except Exception:
                pass

    try:
        FIXTURE_PATH.unlink(missing_ok=True)
        GENERATED_DIR.rmdir()
    except OSError:
        pass
