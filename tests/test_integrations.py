"""Downstream webhook integration CRUD and notification scope checks."""

from __future__ import annotations

import platform
import time
from pathlib import Path
from typing import Any

from agent.container import get_docker_gateway
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("integrations", group="core", order=170)
SIM_PORT = 19016
SIM_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-a.json"
BAD_WEBHOOK_URL = "http://127.0.0.1:19099/webhook"
NOTIFY_SETTLE_SECONDS = 5.0


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}"


def _simulator_address() -> tuple[str, str | None]:
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{SIM_PORT}"
    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{SIM_PORT}"
    return "127.0.0.1", None


def _create(
    client: M3UndleClient, *, name: str, profile_id: str | None
) -> tuple[int, dict[str, Any]]:
    status, body = client.post(
        "/api/v1/downstream/integrations",
        {
            "profileId": profile_id,
            "name": name,
            "kind": "webhook",
            "baseUrl": BAD_WEBHOOK_URL,
            "triggerOnLineupUpdate": True,
            "triggerOnGuideUpdate": True,
            "enabled": True,
        },
    )
    return status, body if isinstance(body, dict) else {}


def _get(client: M3UndleClient, integration_id: str) -> tuple[int, dict[str, Any]]:
    status, body = client.get(f"/api/v1/downstream/integrations/{integration_id}")
    return status, body if isinstance(body, dict) else {}


def _delete(client: M3UndleClient, integration_id: str) -> int:
    status, _ = client.delete(f"/api/v1/downstream/integrations/{integration_id}")
    return status


def _cleanup(client: M3UndleClient, integration_id: str) -> None:
    if integration_id:
        try:
            _delete(client, integration_id)
        except Exception:
            pass


def _refresh(client: M3UndleClient) -> bool:
    previous = str(client.get_snapshot_status_info().get("startedUtc") or "")
    client.trigger_refresh()
    return client.poll_snapshot_status(timeout_seconds=180.0, previous_started_utc=previous)


def _toggle_profile_lineup(client: M3UndleClient, profile_id: str) -> tuple[bool, str]:
    status, filters = client.get(f"/api/v1/profiles/{profile_id}/group-filters")
    if status != 200 or not isinstance(filters, list):
        return False, f"group filters returned {status}"
    selected = next(
        (
            item
            for item in filters
            if isinstance(item, dict)
            and item.get("profileGroupFilterId")
            and item.get("decision") in ("include", "exclude")
        ),
        None,
    )
    if not isinstance(selected, dict):
        return False, "no toggleable group filter found"
    filter_id = str(selected["profileGroupFilterId"])
    current = str(selected["decision"])
    target = "exclude" if current != "exclude" else "include"
    patch_status, patch_body = client._request(
        "PATCH", f"/api/v1/profiles/{profile_id}/group-filters/{filter_id}", body={"decision": target}
    )
    if patch_status != 200:
        return False, f"group-filter PATCH returned {patch_status}: {patch_body}"
    if not _refresh(client):
        return False, "snapshot refresh did not complete"
    return True, f"group_filter={filter_id} decision={current}->{target}"


def _ready(ctx: Any, state: dict[str, object], test_id: str) -> M3UndleClient | None:
    client = state.get("client")
    if state.get("ready") and isinstance(client, M3UndleClient):
        return client
    ctx.skip(test_id, str(state.get("reason") or "Integrations setup did not complete"))
    return None


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    state: dict[str, object] = {
        "ready": False,
        "reason": "Integrations suite setup did not complete",
        "client": None,
        "simulator": None,
        "profile_b_id": None,
    }
    try:
        bind, public_host = _simulator_address()
        simulator = SimulatorInstance(
            fixture=SIM_FIXTURE, port=SIM_PORT, bind=bind, public_host=public_host, suite="integrations"
        )
        state["simulator"] = simulator
        simulator.start()
        if not simulator.wait_healthy():
            state["reason"] = "Provider simulator did not become healthy"
            return {"state": state}
        client = M3UndleClient(base_url)
        state["client"] = client
        if not client.setup(playlist_url=simulator.playlist_url, provider_name=_unique_name("provider-int-a")):
            state["reason"] = f"Provider setup failed: {client.last_setup_error}"
            return {"state": state}
        if not client.profile_id:
            state["reason"] = "Provider setup did not return an active profile"
            return {"state": state}
        state["profile_a_id"] = str(client.profile_id)
        state["ready"] = True
    except Exception as exc:
        state["reason"] = f"Integrations suite setup failed: {exc}"
    return {"state": state}


@SUITE.case("INT-01")
def int_01(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "INT-01")
    if client is None:
        return
    integration_id = ""
    try:
        status, created = _create(client, name=_unique_name("int-crud"), profile_id=None)
        integration_id = str(created.get("downstreamIntegrationId") or "")
        if status != 201 or not integration_id:
            ctx.record("INT-01", False, f"create returned {status}: {created}")
            return
        list_status, listed = client.get("/api/v1/downstream/integrations")
        appears = list_status == 200 and isinstance(listed, list) and any(
            isinstance(item, dict) and str(item.get("downstreamIntegrationId") or "") == integration_id
            for item in listed
        )
        get_status, fetched = _get(client, integration_id)
        fields_match = (
            get_status == 200
            and fetched.get("profileId") is None
            and fetched.get("kind") == "webhook"
            and fetched.get("baseUrl") == BAD_WEBHOOK_URL
            and fetched.get("triggerOnLineupUpdate") is True
            and fetched.get("triggerOnGuideUpdate") is True
            and fetched.get("enabled") is True
        )
        updated_name = _unique_name("int-crud-updated")
        update_status, updated = client._request(
            "PUT",
            f"/api/v1/downstream/integrations/{integration_id}",
            body={
                "name": updated_name,
                "baseUrl": BAD_WEBHOOK_URL,
                "triggerOnLineupUpdate": True,
                "triggerOnGuideUpdate": True,
                "enabled": True,
            },
        )
        deleted = _delete(client, integration_id)
        integration_id = ""
        missing_status, _ = _get(client, str(created["downstreamIntegrationId"]))
        valid = appears and fields_match and update_status == 200 and isinstance(updated, dict) and updated.get("name") == updated_name and deleted in (200, 204) and missing_status == 404
        ctx.record("INT-01", valid, f"list={appears} fields={fields_match} update={update_status} delete={deleted} get_after_delete={missing_status}")
    except Exception as exc:
        ctx.fail("INT-01", str(exc))
    finally:
        _cleanup(client, integration_id)


@SUITE.case("INT-02")
def int_02(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "INT-02")
    profile_id = state.get("profile_a_id")
    if client is None or not isinstance(profile_id, str):
        return
    integration_id = ""
    try:
        status, created = _create(client, name=_unique_name("int-global"), profile_id=None)
        integration_id = str(created.get("downstreamIntegrationId") or "")
        if status != 201 or not integration_id:
            ctx.record("INT-02", False, f"create returned {status}: {created}")
            return
        changed, detail = _toggle_profile_lineup(client, profile_id)
        if not changed:
            ctx.record("INT-02", False, detail)
            return
        time.sleep(NOTIFY_SETTLE_SECONDS)
        fetched_status, fetched = _get(client, integration_id)
        valid = fetched_status == 200 and fetched.get("lastNotifyError") not in (None, "") and fetched.get("lastNotifiedUtc") is None
        ctx.record("INT-02", valid, f"{detail} status={fetched_status} error={fetched.get('lastNotifyError')!r}")
    except Exception as exc:
        ctx.fail("INT-02", str(exc))
    finally:
        _cleanup(client, integration_id)


@SUITE.case("INT-03")
def int_03(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "INT-03")
    profile_a_id = state.get("profile_a_id")
    if client is None or not isinstance(profile_a_id, str):
        return
    integration_b_id = ""
    integration_global_id = ""
    try:
        create_status, profile = client.post("/api/v1/profiles", {"name": _unique_name("profile-int-b")})
        profile_b_id = str(profile.get("profileId") or profile.get("id") or "") if isinstance(profile, dict) else ""
        state["profile_b_id"] = profile_b_id
        if create_status not in (200, 201) or not profile_b_id:
            ctx.record("INT-03", False, f"Could not create second profile: status={create_status}")
            return
        status_b, created_b = _create(client, name=_unique_name("int-b"), profile_id=profile_b_id)
        integration_b_id = str(created_b.get("downstreamIntegrationId") or "")
        status_global, created_global = _create(client, name=_unique_name("int-global"), profile_id=None)
        integration_global_id = str(created_global.get("downstreamIntegrationId") or "")
        if status_b != 201 or not integration_b_id or status_global != 201 or not integration_global_id:
            ctx.record("INT-03", False, f"create profile/global returned {status_b}/{status_global}")
            return
        changed, detail = _toggle_profile_lineup(client, profile_a_id)
        if not changed:
            ctx.record("INT-03", False, detail)
            return
        time.sleep(NOTIFY_SETTLE_SECONDS)
        status_b_get, profile_b = _get(client, integration_b_id)
        status_global_get, global_integration = _get(client, integration_global_id)
        suppressed = status_b_get == 200 and profile_b.get("lastNotifyError") is None
        global_attempted = status_global_get == 200 and global_integration.get("lastNotifyError") not in (None, "")
        ctx.record("INT-03", suppressed and global_attempted, f"{detail} profile_b_error={profile_b.get('lastNotifyError')!r} global_error={global_integration.get('lastNotifyError')!r}")
    except Exception as exc:
        ctx.fail("INT-03", str(exc))
    finally:
        _cleanup(client, integration_b_id)
        _cleanup(client, integration_global_id)


@SUITE.case("INT-04")
def int_04(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "INT-04")
    profile_id = state.get("profile_a_id")
    if client is None or not isinstance(profile_id, str):
        return
    integration_id = ""
    try:
        status, created = _create(client, name=_unique_name("int-a"), profile_id=profile_id)
        integration_id = str(created.get("downstreamIntegrationId") or "")
        if status != 201 or not integration_id:
            ctx.record("INT-04", False, f"create returned {status}: {created}")
            return
        changed, detail = _toggle_profile_lineup(client, profile_id)
        if not changed:
            ctx.record("INT-04", False, detail)
            return
        time.sleep(NOTIFY_SETTLE_SECONDS)
        fetched_status, fetched = _get(client, integration_id)
        valid = fetched_status == 200 and fetched.get("lastNotifyError") not in (None, "") and fetched.get("lastNotifiedUtc") is None
        ctx.record("INT-04", valid, f"{detail} status={fetched_status} error={fetched.get('lastNotifyError')!r}")
    except Exception as exc:
        ctx.fail("INT-04", str(exc))
    finally:
        _cleanup(client, integration_id)


@SUITE.teardown
def teardown(base_url: str, state: dict[str, object]) -> None:
    client = state.get("client")
    profile_b_id = state.get("profile_b_id")
    if isinstance(client, M3UndleClient) and isinstance(profile_b_id, str) and profile_b_id:
        try:
            client.wait_snapshot_idle(timeout_seconds=60.0)
            client._request("DELETE", f"/api/v1/profiles/{profile_b_id}")
        except Exception:
            pass
    simulator = state.get("simulator")
    if isinstance(simulator, SimulatorInstance):
        try:
            simulator.stop()
        except Exception:
            pass
