"""EPG source management and XMLTV publication checks."""

from __future__ import annotations

from datetime import UTC, datetime
import platform
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from agent import common as lab_common
from agent.container import get_docker_gateway
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("epg", group="core", order=180)
SIM_PORT = 19015
SIM_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-epg.json"


def _simulator_address() -> tuple[str, str | None]:
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{SIM_PORT}"
    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{SIM_PORT}"
    return "127.0.0.1", None


def _source_id(source: dict[str, Any]) -> str:
    return str(source.get("epgSourceId") or source.get("sourceId") or source.get("id") or "")


def _sources(client: M3UndleClient) -> list[dict[str, Any]]:
    status, body = client.get("/api/v1/epg/sources")
    return [item for item in body if isinstance(item, dict)] if status == 200 and isinstance(body, list) else []


def _source(client: M3UndleClient, source_id: str) -> tuple[int, dict[str, Any]]:
    found = next((item for item in _sources(client) if _source_id(item) == source_id), None)
    return (200, found) if isinstance(found, dict) else (404, {})


def _runs(client: M3UndleClient, source_id: str) -> list[dict[str, Any]]:
    status, body = client.get(f"/api/v1/epg/sources/{source_id}/fetch-runs")
    return [item for item in body if isinstance(item, dict)] if status == 200 and isinstance(body, list) else []


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _refresh(client: M3UndleClient) -> bool:
    previous = str(client.get_snapshot_status_info().get("startedUtc") or "")
    client.trigger_refresh()
    return client.poll_snapshot_status(timeout_seconds=120.0, previous_started_utc=previous)


def _ready(ctx: Any, state: dict[str, object], test_id: str) -> M3UndleClient | None:
    client = state.get("client")
    if state.get("ready") and isinstance(client, M3UndleClient):
        return client
    ctx.skip(test_id, str(state.get("reason") or "EPG suite setup did not complete"))
    return None


def _source_ready(ctx: Any, state: dict[str, object], test_id: str) -> tuple[M3UndleClient, str] | None:
    client = _ready(ctx, state, test_id)
    source_id = state.get("source_id")
    if client is not None and isinstance(source_id, str) and source_id:
        return client, source_id
    if client is not None:
        ctx.skip(test_id, "Skipped because EPG source creation did not complete")
    return None


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    state: dict[str, object] = {"ready": False, "reason": "EPG setup did not complete", "client": None, "simulator": None}
    try:
        bind, public_host = _simulator_address()
        simulator = SimulatorInstance(fixture=SIM_FIXTURE, port=SIM_PORT, bind=bind, public_host=public_host, suite="epg")
        state["simulator"] = simulator
        simulator.start(log_path=lab_common.runtime_results_dir() / "sim-epg.log")
        if not simulator.wait_healthy():
            state["reason"] = "EPG simulator did not become healthy"
            return {"state": state}
        client = M3UndleClient(base_url)
        state["client"] = client
        if not client.setup(playlist_url=simulator.playlist_url, provider_name=f"provider-epg-sim-{int(time.time())}"):
            state["reason"] = f"Provider setup failed: {client.last_setup_error}"
            return {"state": state}
        state["epg_url"] = f"{simulator.public_host}/epg/guide.xml"
        state["ready"] = True
    except Exception as exc:
        state["reason"] = f"EPG suite setup failed: {exc}"
    return {"state": state}


@SUITE.case("EPG-01")
def epg_01(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "EPG-01")
    url = state.get("epg_url")
    if client is None or not isinstance(url, str):
        return
    status, body = client.post("/api/v1/epg/sources", {"providerId": str(client.provider_id or ""), "name": f"sim-epg-{int(time.time())}", "kind": "xmltv_url", "urlOrPath": url, "enabled": True, "priority": 10})
    source_id = _source_id(body) if isinstance(body, dict) else ""
    valid = status in (200, 201) and bool(source_id)
    ctx.record("EPG-01", valid, f"status={status} source_id={source_id!r}")
    if valid:
        state["source_id"] = source_id


@SUITE.case("EPG-02")
def epg_02(ctx: Any, state: dict[str, object]) -> None:
    ready = _source_ready(ctx, state, "EPG-02")
    if ready is None:
        return
    client, source_id = ready
    status, body = client.post(f"/api/v1/epg/sources/{source_id}/test")
    source = body if isinstance(body, dict) else {}
    has_channels = any(key in source for key in ("channels", "channelCount", "channel_count"))
    valid = status in (200, 202) and has_channels and str(source.get("providerId") or "") == str(client.provider_id or "") and source.get("cachedXmlAvailable") is True
    ctx.record("EPG-02", valid, f"status={status} keys={list(source)[:10]} cachedXmlAvailable={source.get('cachedXmlAvailable')}")


@SUITE.case("EPG-03")
def epg_03(ctx: Any, state: dict[str, object]) -> None:
    ready = _source_ready(ctx, state, "EPG-03")
    if ready is None:
        return
    client, source_id = ready
    status, body = client.post(f"/api/v1/epg/sources/{source_id}/auto-map")
    maps_status, mappings = client.get(f"/api/v1/epg/mappings?profileId={client.profile_id}")
    count = len(mappings) if maps_status == 200 and isinstance(mappings, list) else 0
    ctx.record("EPG-03", status in (200, 201, 202, 204) and maps_status == 200 and count > 0, f"auto-map={status} mappings={count} body={body}")


@SUITE.case("EPG-04")
def epg_04(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "EPG-04")
    if client is None:
        return
    try:
        if not _refresh(client):
            raise RuntimeError("Snapshot refresh did not complete")
        with urlopen(Request(f"{client.base_url}/xmltv/m3undle.xml", headers={"Accept": "application/xml, text/xml"}), timeout=15) as response:
            root = ElementTree.fromstring(response.read())
        programmes = root.findall(".//{*}programme") or root.findall(".//programme")
        channels = root.findall(".//{*}channel") or root.findall(".//channel")
        ctx.record("EPG-04", len(programmes) > 0, f"channels={len(channels)} programmes={len(programmes)}")
    except Exception as exc:
        ctx.fail("EPG-04", str(exc))


@SUITE.case("EPG-05")
def epg_05(ctx: Any, state: dict[str, object]) -> None:
    ready = _source_ready(ctx, state, "EPG-05")
    if ready is None:
        return
    client, source_id = ready
    mapping_id = ""
    try:
        status, channels = client.get(f"/api/v1/profiles/{client.profile_id}/channels?page=1&pageSize=10")
        items = channels.get("items") if status == 200 and isinstance(channels, dict) else []
        first = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
        provider_channel_id = first.get("providerChannelId")
        if not provider_channel_id:
            raise RuntimeError(f"No provider channel ID in {first}")
        put_status, created = client._request("PUT", "/api/v1/epg/mappings", body={"profileId": str(client.profile_id or ""), "providerChannelId": provider_channel_id, "epgSourceId": source_id, "xmltvChannelId": "epg-sports-1"})
        mapping = created if isinstance(created, dict) else {}
        mapping_id = str(mapping.get("epgChannelMappingId") or mapping.get("mappingId") or mapping.get("id") or "")
        get_status, _ = client.get("/api/v1/epg/mappings")
        delete_status = client._request("DELETE", f"/api/v1/epg/mappings/{mapping_id}")[0] if mapping_id else 0
        mapping_id = ""
        ctx.record("EPG-05", put_status in (200, 201) and get_status == 200 and delete_status in (200, 204), f"create={put_status} read={get_status} delete={delete_status}")
    except Exception as exc:
        ctx.fail("EPG-05", str(exc))
    finally:
        if mapping_id:
            try:
                client._request("DELETE", f"/api/v1/epg/mappings/{mapping_id}")
            except Exception:
                pass


@SUITE.case("EPG-06")
def epg_06(ctx: Any, state: dict[str, object]) -> None:
    ready = _source_ready(ctx, state, "EPG-06")
    if ready is None:
        return
    client, source_id = ready
    status, body = client.post(f"/api/v1/epg/sources/{source_id}/fetch")
    fetch = body if isinstance(body, dict) else {}
    run_id = str(fetch.get("epgFetchRunId") or "")
    matching = next((run for run in _runs(client, source_id) if str(run.get("epgFetchRunId") or "") == run_id), None)
    valid = status == 200 and bool(run_id) and isinstance(matching, dict) and str(matching.get("status") or "") in ("ok", "not_modified")
    ctx.record("EPG-06", valid, f"fetchRunId={run_id!r} runs={len(_runs(client, source_id))} status={matching.get('status') if isinstance(matching, dict) else None}")


@SUITE.case("EPG-07")
def epg_07(ctx: Any, state: dict[str, object]) -> None:
    ready = _source_ready(ctx, state, "EPG-07")
    if ready is None:
        return
    client, _ = ready
    provider_id = str(client.provider_id or "")
    status, body = client.get(f"/api/v1/epg/sources?providerId={provider_id}")
    if status == 501:
        ctx.skip("EPG-07", "Provider filter is not implemented")
        return
    filtered = body if isinstance(body, list) else []
    valid = status == 200 and all(isinstance(item, dict) and str(item.get("providerId") or "") == provider_id for item in filtered)
    ctx.record("EPG-07", valid, f"filtered={len(filtered)} status={status}")


@SUITE.case("EPG-08")
def epg_08(ctx: Any, state: dict[str, object]) -> None:
    ready = _source_ready(ctx, state, "EPG-08")
    if ready is None:
        return
    client, source_id = ready
    try:
        source_status, source = _source(client, source_id)
        if source_status != 200:
            raise RuntimeError(f"Source lookup returned {source_status}")
        patch = {"name": source.get("name"), "urlOrPath": source.get("urlOrPath"), "priority": source.get("priority"), "enabled": source.get("enabled"), "headersJson": source.get("headersJson"), "userAgent": source.get("userAgent"), "timeoutSeconds": source.get("timeoutSeconds"), "refreshIntervalHoursSet": True, "refreshIntervalHours": 24}
        patch_status, patch_body = client._request("PATCH", f"/api/v1/epg/sources/{source_id}", body=patch)
        if patch_status != 200:
            raise RuntimeError(f"Source PATCH returned {patch_status}: {patch_body}")
        if not _refresh(client):
            raise RuntimeError("Initial refresh did not complete")
        prior_ids = {str(run.get("epgFetchRunId") or "") for run in _runs(client, source_id)}
        started = datetime.now(UTC)
        if not _refresh(client):
            raise RuntimeError("Cadence refresh did not complete")
        new_runs = [run for run in _runs(client, source_id) if str(run.get("epgFetchRunId") or "") not in prior_ids]
        latest = max(new_runs, key=lambda run: _parse_utc(run.get("startedUtc")) or datetime.min.replace(tzinfo=UTC), default=None)
        _, current = _source(client, source_id)
        latest_started = _parse_utc(latest.get("startedUtc")) if isinstance(latest, dict) else None
        last_success = _parse_utc(current.get("lastSuccessUtc"))
        valid = bool(new_runs) and isinstance(latest, dict) and latest.get("status") == "not_modified" and latest_started is not None and latest_started >= started and last_success is not None and last_success >= started
        ctx.record("EPG-08", valid, f"new_runs={len(new_runs)} latest_status={latest.get('status') if isinstance(latest, dict) else None} lastSuccessUtc={current.get('lastSuccessUtc')}")
    except Exception as exc:
        ctx.fail("EPG-08", str(exc))


@SUITE.teardown
def teardown(state: dict[str, object]) -> None:
    simulator = state.get("simulator")
    if isinstance(simulator, SimulatorInstance):
        try:
            simulator.stop()
        except Exception:
            pass
