"""Port of the frozen destructive backup/restore round-trip suite."""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from agent import common as lab_common, registry
from agent.container import container_started_at, get_docker_gateway, wait_for_restart, wait_up
from agent.suites import suite

from m3undle_lab.api import M3UndleClient, backup_api_capability
from m3undle_lab.commands import CONTAINER_NAME, HOST_OVERRIDE, SERVICE
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("backup-restore", group="core", order=140)
SIM_PORT = 19027
SIM_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-a.json"
ENCRYPTION_KEY_ENV = "M3UNDLE_ENCRYPTION_KEY"
ENCRYPTION_KEYS_ENV = "M3UNDLE_ENCRYPTION_KEYS"
TEMP_KEY = base64.b64encode(b"m3undle-lab-backup-restore-key!!").decode("ascii")


def _simulator_address() -> tuple[str, str | None]:
    """Return the listener and advertised URL suitable for this Docker host."""
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{SIM_PORT}"

    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{SIM_PORT}"
    return "127.0.0.1", None


def _safe_target(base_url: str) -> str | None:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != 8080:
        return f"Refusing destructive backup/restore test against non-lab target {base_url!r}"
    return None


def _canonical_state(client: M3UndleClient, profile_id: str) -> dict[str, Any]:
    provider_status, providers = client.get("/api/v1/providers")
    profile_status, profiles = client.get("/api/v1/profiles")
    filter_status, filters = client.get(f"/api/v1/profiles/{profile_id}/group-filters")
    hdhr_status, hdhr = client.get("/api/v1/settings/hdhr")
    events_status, events = client.get("/api/v1/settings/events")
    if any(status != 200 for status in (provider_status, profile_status, filter_status, hdhr_status, events_status)):
        raise RuntimeError("One or more canonical-state APIs did not return 200")
    if not all(isinstance(value, list) for value in (providers, profiles, filters)):
        raise RuntimeError("Canonical-state API returned an unexpected collection")
    if not isinstance(hdhr, dict) or not isinstance(events, dict):
        raise RuntimeError("Canonical-state settings API returned an unexpected response")

    normalized_filters: list[dict[str, Any]] = []
    for item in filters:
        if not isinstance(item, dict):
            raise RuntimeError("Group filter response contained a non-object")
        filter_id = str(item["profileGroupFilterId"])
        selection_status, selections = client.get(
            f"/api/v1/profiles/{profile_id}/group-filters/{filter_id}/channel-selections"
        )
        if selection_status != 200 or not isinstance(selections, dict):
            raise RuntimeError(f"Channel selections returned {selection_status} for {filter_id}")
        channels = selections.get("channels", [])
        if not isinstance(channels, list):
            raise RuntimeError(f"Channel selections were invalid for {filter_id}")
        normalized_filters.append(
            {
                "id": filter_id,
                "providerGroupId": item.get("providerGroupId"),
                "rawName": item.get("providerGroupRawName"),
                "decision": item.get("decision"),
                "channelMode": item.get("channelMode"),
                "outputName": item.get("outputName"),
                "channels": sorted(
                    [
                        {
                            "id": channel.get("providerChannelId"),
                            "name": channel.get("displayName"),
                            "state": channel.get("state"),
                            "outputGroupName": channel.get("outputGroupName"),
                            "channelNumber": channel.get("channelNumber"),
                        }
                        for channel in channels
                        if isinstance(channel, dict)
                    ],
                    key=lambda channel: str(channel["id"]),
                ),
            }
        )
    return {
        "providers": sorted(
            [{"providerId": item.get("providerId"), "name": item.get("name")} for item in providers if isinstance(item, dict)],
            key=lambda item: str(item["providerId"]),
        ),
        "profiles": sorted(
            [
                {
                    "profileId": item.get("profileId"),
                    "name": item.get("name"),
                    "enabled": item.get("enabled"),
                    "isActive": item.get("isActive"),
                }
                for item in profiles
                if isinstance(item, dict)
            ],
            key=lambda item: str(item["profileId"]),
        ),
        "filters": sorted(normalized_filters, key=lambda item: str(item["id"])),
        "settings": {
            "hdhrFriendlyName": hdhr.get("friendlyName"),
            "hdhrTunerCountOverride": hdhr.get("tunerCountOverride"),
            "eventRetentionDays": events.get("retentionDays"),
        },
    }


def _configure_mapping(client: M3UndleClient, profile_id: str) -> None:
    status, filters = client.get(f"/api/v1/profiles/{profile_id}/group-filters")
    if status != 200 or not isinstance(filters, list) or len(filters) != 2:
        raise RuntimeError(f"Expected two provider groups, got status={status} body={filters}")
    for item in filters:
        if not isinstance(item, dict):
            raise RuntimeError("Group filter response contained a non-object")
        filter_id = str(item["profileGroupFilterId"])
        raw_name = str(item["providerGroupRawName"])
        patch: dict[str, str] = {"decision": "include", "channelMode": "select"}
        if raw_name == "Sim-Sports":
            patch["outputName"] = "Restored Sports"
        patch_status, _ = client._request("PATCH", f"/api/v1/profiles/{profile_id}/group-filters/{filter_id}", body=patch)
        if patch_status != 200:
            raise RuntimeError(f"Group update returned {patch_status} for {raw_name}")
        selection_status, selections = client.get(
            f"/api/v1/profiles/{profile_id}/group-filters/{filter_id}/channel-selections"
        )
        if selection_status != 200 or not isinstance(selections, dict):
            raise RuntimeError(f"Selection read returned {selection_status} for {raw_name}")
        channels = selections.get("channels", [])
        if not isinstance(channels, list):
            raise RuntimeError(f"Selection channels were invalid for {raw_name}")
        requested = [
            {
                "providerChannelId": channel["providerChannelId"],
                "state": "excluded" if raw_name == "Sim-Sports" and index == len(channels) - 1 else "included",
            }
            for index, channel in enumerate(channels)
            if isinstance(channel, dict)
        ]
        update_status, _ = client._request(
            "PUT",
            f"/api/v1/profiles/{profile_id}/group-filters/{filter_id}/channel-selections",
            body={"channelMode": "select", "channels": requested},
        )
        if update_status != 200:
            raise RuntimeError(f"Selection update returned {update_status} for {raw_name}")
    client.build_snapshot()
    if not client.poll_build_completion(profile_id):
        raise RuntimeError("Configured snapshot build did not complete")


def _update_settings(client: M3UndleClient) -> None:
    status, current = client.get("/api/v1/settings/hdhr")
    if status != 200 or not isinstance(current, dict):
        raise RuntimeError(f"HDHR settings read returned {status}")
    update_status, body = client._request(
        "PUT",
        "/api/v1/settings/hdhr",
        body={
            "enabled": current.get("enabled", True),
            "tunerCountOverride": 7,
            "friendlyName": "M3Undle Backup Lab",
            "advertisedBaseUrl": current.get("advertisedBaseUrl"),
            "discoveryEnabled": current.get("discoveryEnabled", True),
            "ssdpEnabled": current.get("ssdpEnabled", True),
            "siliconDustDiscoveryEnabled": current.get("siliconDustDiscoveryEnabled", True),
            "allowedNetworks": current.get("allowedNetworks"),
        },
    )
    if update_status != 200:
        raise RuntimeError(f"HDHR settings update returned {update_status}: {body}")
    event_status, event_body = client._request("PUT", "/api/v1/settings/events", body={"retentionDays": 37})
    if event_status != 200:
        raise RuntimeError(f"Event settings update returned {event_status}: {event_body}")


def _restart_stack(base_url: str) -> bool:
    lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
    return wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health"))


def _restore_environment(base_url: str, state: dict[str, object]) -> tuple[bool, str]:
    original = state.get("original_env")
    if not isinstance(original, dict):
        return True, "No encryption environment needed restoration"
    updates: dict[str, str] = {}
    for key, value in original.items():
        if value is None:
            lab_common.unset_runtime_env_var(str(key))
        else:
            updates[str(key)] = str(value)
    if updates:
        lab_common.write_env_file_values(lab_common.runtime_env_file(), updates)
    try:
        return _restart_stack(base_url), "Original encryption environment restored"
    except Exception as exc:
        return False, f"Could not restart with original encryption environment: {exc}"


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    state: dict[str, object] = {
        "ready": False,
        "reason": _safe_target(base_url) or "Backup/restore suite setup did not complete",
        "client": None,
        "simulator": None,
        "artifacts": lab_common.runtime_results_dir(),
        "original_env": None,
    }
    if _safe_target(base_url):
        return {"state": state}
    try:
        client = M3UndleClient(base_url)
        state["client"] = client
        capability, detail = backup_api_capability(*client.get("/api/v1/backups/"))
        if capability != "supported":
            state["reason"] = detail
            return {"state": state}
        bind, public_host = _simulator_address()
        simulator = SimulatorInstance(fixture=SIM_FIXTURE, port=SIM_PORT, bind=bind, public_host=public_host, suite="backup-restore")
        state["simulator"] = simulator
        simulator.start()
        if not simulator.wait_healthy():
            state["reason"] = "Provider simulator did not become healthy"
            return {"state": state}
        artifacts = state["artifacts"]
        if isinstance(artifacts, Path):
            artifacts.mkdir(parents=True, exist_ok=True)
        state["ready"] = True
    except Exception as exc:
        state["reason"] = f"Backup/restore suite setup failed: {exc}"
    return {"state": state}


def _ready(ctx: Any, state: dict[str, object], test_id: str) -> tuple[M3UndleClient, Path] | None:
    client = state.get("client")
    artifacts = state.get("artifacts")
    if state.get("ready") and isinstance(client, M3UndleClient) and isinstance(artifacts, Path):
        return client, artifacts
    ctx.skip(test_id, str(state.get("reason") or "Backup/restore suite setup did not complete"))
    return None


def _prior_ok(ctx: Any, state: dict[str, object], test_id: str, required: str) -> bool:
    if state.get(required):
        return True
    ctx.skip(test_id, f"Skipped because prerequisite {required} did not complete")
    return False


@SUITE.case("BKR-01")
def bkr_01(ctx: Any, base_url: str, state: dict[str, object]) -> None:
    ready = _ready(ctx, state, "BKR-01")
    if ready is None:
        return
    client, artifacts = ready
    try:
        current_env = lab_common.load_env_file(lab_common.runtime_env_file())
        state["original_env"] = {key: current_env.get(key) for key in (ENCRYPTION_KEY_ENV, ENCRYPTION_KEYS_ENV)}
        if not current_env.get(ENCRYPTION_KEY_ENV) and not current_env.get(ENCRYPTION_KEYS_ENV):
            lab_common.set_runtime_env_values({ENCRYPTION_KEY_ENV: TEMP_KEY})
            if not _restart_stack(base_url):
                raise RuntimeError("M3Undle did not restart with the temporary encryption key")
        simulator = state.get("simulator")
        if not isinstance(simulator, SimulatorInstance):
            raise RuntimeError("Provider simulator was unavailable")
        if not client.setup(playlist_url=simulator.playlist_url, provider_name=f"backup-restore-{int(time.time())}"):
            raise RuntimeError(f"Provider setup failed: {client.last_setup_error}")
        profile_id = str(client.profile_id or "")
        if not profile_id:
            raise RuntimeError("Provider setup did not return a profile")
        _configure_mapping(client, profile_id)
        _update_settings(client)
        before = _canonical_state(client, profile_id)
        (artifacts / "backup-restore-before.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
        state["before"] = before
        state["profile_id"] = profile_id
        state["configured"] = True
        ctx.record("BKR-01", True, "Configured provider, profile, mappings, and persisted settings")
    except Exception as exc:
        ctx.fail("BKR-01", str(exc))


@SUITE.case("BKR-02")
def bkr_02(ctx: Any, state: dict[str, object]) -> None:
    ready = _ready(ctx, state, "BKR-02")
    if ready is None or not _prior_ok(ctx, state, "BKR-02", "configured"):
        return
    client, artifacts = ready
    create_status, created = client.post("/api/v1/backups", timeout=120.0)
    file_name = created.get("fileName") if isinstance(created, dict) else None
    encoded = urllib.parse.quote(str(file_name), safe="") if file_name else ""
    validate_status, validated = client.post(f"/api/v1/backups/{encoded}/validate", timeout=120.0) if file_name else (0, {})
    download_status, archive_bytes = client.download_bytes(f"/api/v1/backups/{encoded}/download", timeout=120.0) if file_name else (0, b"")
    archive_path = artifacts / "backup-restore-source.m3undle-backup"
    archive_path.write_bytes(archive_bytes)
    valid = (
        create_status == 200
        and validate_status == 200
        and isinstance(validated, dict)
        and validated.get("success") is True
        and download_status == 200
    )
    detail = f"create={create_status} validate={validate_status} download={download_status}"
    if valid:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            (artifacts / "backup-restore-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            (artifacts / "backup-restore-source.sha256").write_text(
                hashlib.sha256(archive_bytes).hexdigest() + "\n", encoding="utf-8"
            )
        except Exception as exc:
            valid = False
            detail = f"{detail}; archive inspection failed: {exc}"
    ctx.record("BKR-02", valid, detail)
    if valid:
        state["archive_path"] = archive_path
        state["archived"] = True


@SUITE.case("BKR-03")
def bkr_03(ctx: Any, base_url: str, state: dict[str, object]) -> None:
    ready = _ready(ctx, state, "BKR-03")
    if ready is None or not _prior_ok(ctx, state, "BKR-03", "archived"):
        return
    client, _ = ready
    try:
        lab_common.run(lab_common.compose_command("down", "--remove-orphans", extra_compose_files=[HOST_OVERRIDE]), check=False)
        registry.get_database_plugin().reset()
        lab_common.compose_up_only(SERVICE, extra_compose_files=[HOST_OVERRIDE])
        if not wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health")):
            raise RuntimeError("Clean-database M3Undle did not become healthy")
        status, providers = client.get("/api/v1/providers")
        clean = status == 200 and providers == []
        ctx.record("BKR-03", clean, "Started a newly migrated empty database")
        state["database_reset"] = clean
    except Exception as exc:
        ctx.fail("BKR-03", str(exc))


@SUITE.case("BKR-04")
def bkr_04(ctx: Any, base_url: str, state: dict[str, object]) -> None:
    ready = _ready(ctx, state, "BKR-04")
    if ready is None or not _prior_ok(ctx, state, "BKR-04", "database_reset"):
        return
    client, _ = ready
    archive_path = state.get("archive_path")
    if not isinstance(archive_path, Path):
        ctx.skip("BKR-04", "Skipped because the backup archive was unavailable")
        return
    upload_status, uploaded = client.upload_file("/api/v1/backups/upload", archive_path)
    uploaded_name = uploaded.get("fileName") if isinstance(uploaded, dict) else None
    stage_status, staged = client.post("/api/v1/restore/stage", {"fileName": uploaded_name}, timeout=120.0)
    started_at = container_started_at(CONTAINER_NAME)
    confirm_status, _ = client.post("/api/v1/restore/confirm")
    restart_detected = confirm_status == 200 and wait_for_restart(CONTAINER_NAME, started_at)
    healthy = restart_detected and wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health"))
    status, restore_status = client.get("/api/v1/restore/status") if healthy else (0, {})
    restored = (
        upload_status == 200
        and isinstance(uploaded, dict)
        and uploaded.get("valid") is True
        and stage_status == 200
        and isinstance(staged, dict)
        and staged.get("success") is True
        and healthy
        and status == 200
        and isinstance(restore_status, dict)
        and restore_status.get("state") == "Completed"
    )
    ctx.record(
        "BKR-04",
        restored,
        f"upload={upload_status} valid={uploaded.get('valid') if isinstance(uploaded, dict) else None} "
        f"stage={stage_status} staged={staged.get('success') if isinstance(staged, dict) else None} "
        f"confirm={confirm_status} restart_detected={restart_detected} healthy={healthy} "
        f"restore_status={status}:{restore_status}",
    )
    state["restored"] = restored


@SUITE.case("BKR-05")
def bkr_05(ctx: Any, state: dict[str, object]) -> None:
    ready = _ready(ctx, state, "BKR-05")
    if ready is None or not _prior_ok(ctx, state, "BKR-05", "restored"):
        return
    client, artifacts = ready
    profile_id = state.get("profile_id")
    before = state.get("before")
    if not isinstance(profile_id, str) or not isinstance(before, dict):
        ctx.skip("BKR-05", "Skipped because pre-restore state was unavailable")
        return
    try:
        if not client.wait_snapshot_idle(timeout_seconds=180.0):
            raise RuntimeError("Post-restore refresh did not settle")
        after = _canonical_state(client, profile_id)
        (artifacts / "backup-restore-after.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
        ctx.record("BKR-05", before == after, "Canonical API state matches before and after restore", {"before": before, "after": after})
        state["state_matched"] = before == after
    except Exception as exc:
        ctx.fail("BKR-05", str(exc))


@SUITE.case("BKR-06")
def bkr_06(ctx: Any, state: dict[str, object]) -> None:
    ready = _ready(ctx, state, "BKR-06")
    if ready is None or not _prior_ok(ctx, state, "BKR-06", "restored"):
        return
    client, _ = ready
    status, lineup = client.download_bytes("/m3u/m3undle.m3u", timeout=60.0)
    text = lineup.decode("utf-8", errors="replace")
    valid = status == 200 and "Channel 101 (Provider A)" in text and "Channel 102 (Provider A)" in text and "Channel 103 (Provider A)" not in text and "Restored Sports" in text
    ctx.record("BKR-06", valid, f"restored M3U status={status} has expected mapped channels")


@SUITE.case("BKR-07")
def bkr_07(ctx: Any, base_url: str, state: dict[str, object]) -> None:
    if _ready(ctx, state, "BKR-07") is None:
        return
    restored, detail = _restore_environment(base_url, state)
    ctx.record("BKR-07", restored, detail if restored else f"{detail}; M3Undle did not become healthy")
    state["environment_restored"] = restored


@SUITE.teardown
def teardown(base_url: str, state: dict[str, object]) -> None:
    if not state.get("environment_restored"):
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
