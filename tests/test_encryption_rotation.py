"""Port of the frozen encryption key-rotation suite."""

from __future__ import annotations

import base64
import platform
import secrets
import time
from pathlib import Path
from typing import Any

from agent import common as lab_common
from agent.container import get_docker_gateway, wait_up
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.commands import CONTAINER_NAME, HOST_OVERRIDE
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("encryption-rotation", group="core", order=150)
SIM_PORT = 19026
SIM_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-xtream.json"
UPSTREAM_XTREAM_USER = "xtreamuser"
UPSTREAM_XTREAM_PASS = "xtreampass"
ENCRYPTION_KEYS_ENV = "M3UNDLE_ENCRYPTION_KEYS"
ENCRYPTION_KEY_ENV = "M3UNDLE_ENCRYPTION_KEY"
ROTATION_KEY_ID = "lab-rotation-test"
FALLBACK_KEY_ID = "legacy"


def _generate_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _simulator_address() -> tuple[str, str | None]:
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{SIM_PORT}"
    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{SIM_PORT}"
    return "127.0.0.1", None


def _restart_with_env(base_url: str, values: dict[str, str]) -> tuple[bool, str]:
    lab_common.set_runtime_env_values(values)
    try:
        lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
    except Exception as exc:
        return False, f"compose up failed: {exc}"
    if not wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health")):
        return False, "stack did not become healthy after restart"
    return True, "restarted and healthy"


def _get_status(client: M3UndleClient) -> tuple[int, dict[str, Any]]:
    status, body = client.get("/api/v1/encryption/status")
    return status, body if isinstance(body, dict) else {}


def _post_rotate(client: M3UndleClient) -> tuple[int, dict[str, Any]]:
    status, body = client.post("/api/v1/encryption/rotate")
    return status, body if isinstance(body, dict) else {}


def _restore_original_environment(base_url: str, state: dict[str, object]) -> tuple[bool, str]:
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
        lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
        started = wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health"))
        return started, "Restored the original encryption-key environment"
    except Exception as exc:
        return False, f"Could not restore original encryption-key environment: {exc}"


def _unwind_rotation(base_url: str, state: dict[str, object]) -> tuple[bool, str]:
    client = state.get("client")
    baseline = state.get("baseline_ring_entry")
    temporary_key = state.get("temporary_key")
    if not isinstance(client, M3UndleClient) or not isinstance(baseline, str) or not isinstance(temporary_key, str):
        return True, "No completed rotation needed unwinding"

    started, detail = _restart_with_env(
        base_url,
        {ENCRYPTION_KEYS_ENV: f"{baseline},{ROTATION_KEY_ID}:{temporary_key}"},
    )
    if not started:
        return False, f"Could not restart with the unwind ring: {detail}"
    status, body = _post_rotate(client)
    if status != 200:
        return False, f"Rotate back onto the original key failed: status={status} body={body}"
    check_status, check_body = _get_status(client)
    settled = (
        check_status == 200
        and check_body.get("activeKeyId") != ROTATION_KEY_ID
        and int(check_body.get("providersOnOtherKey") or 0) == 0
        and int(check_body.get("downstreamIntegrationsOnOtherKey") or 0) == 0
    )
    return settled, f"Moved encrypted rows back off {ROTATION_KEY_ID!r}: status={check_status} body={check_body}"


def _ready(ctx: Any, state: dict[str, object], test_id: str) -> M3UndleClient | None:
    client = state.get("client")
    if state.get("ready") and isinstance(client, M3UndleClient):
        return client
    ctx.skip(test_id, str(state.get("reason") or "Encryption rotation setup did not complete"))
    return None


def _prior_ok(ctx: Any, state: dict[str, object], test_id: str, required: str) -> bool:
    if state.get(required):
        return True
    ctx.skip(test_id, f"Skipped because prerequisite {required} did not complete")
    return False


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    state: dict[str, object] = {
        "ready": False,
        "reason": "Encryption rotation suite setup did not complete",
        "client": None,
        "simulator": None,
        "original_env": None,
        "baseline_ring_entry": None,
        "temporary_key": None,
    }
    try:
        client = M3UndleClient(base_url)
        state["client"] = client
        current_env = lab_common.load_env_file(lab_common.runtime_env_file())
        original = {
            ENCRYPTION_KEYS_ENV: current_env.get(ENCRYPTION_KEYS_ENV),
            ENCRYPTION_KEY_ENV: current_env.get(ENCRYPTION_KEY_ENV),
        }
        state["original_env"] = original
        existing_ring = original[ENCRYPTION_KEYS_ENV]
        existing_single_key = original[ENCRYPTION_KEY_ENV]
        baseline = existing_ring or (
            f"{FALLBACK_KEY_ID}:{existing_single_key}" if existing_single_key else None
        )
        if not baseline:
            baseline = f"{FALLBACK_KEY_ID}:{_generate_key()}"
            started, detail = _restart_with_env(base_url, {ENCRYPTION_KEYS_ENV: baseline})
            if not started:
                state["reason"] = f"Could not seed a starting encryption key: {detail}"
                return {"state": state}
        state["baseline_ring_entry"] = baseline

        bind, public_host = _simulator_address()
        simulator = SimulatorInstance(
            fixture=SIM_FIXTURE,
            port=SIM_PORT,
            bind=bind,
            public_host=public_host,
            suite="encryption-rotation",
        )
        state["simulator"] = simulator
        simulator.start()
        if not simulator.wait_healthy():
            state["reason"] = "Xtream simulator did not become healthy"
            return {"state": state}
        upstream = simulator.public_host or simulator.playlist_url.rsplit("/playlist.m3u", 1)[0]
        provider_name = f"provider-enc-rotation-{int(time.time())}"
        if not client.setup_xtream(
            xtream_base_url=upstream,
            xtream_username=UPSTREAM_XTREAM_USER,
            xtream_password=UPSTREAM_XTREAM_PASS,
            provider_name=provider_name,
        ):
            state["reason"] = f"Xtream provider setup failed: {client.last_setup_error}"
            return {"state": state}
        if not client.provider_id:
            state["reason"] = "Xtream provider setup did not return a provider identifier"
            return {"state": state}
        state["ready"] = True
    except Exception as exc:
        state["reason"] = f"Encryption rotation suite setup failed: {exc}"
    return {"state": state}


@SUITE.case("ENC-01")
def enc_01(ctx: Any, state: dict[str, object]) -> None:
    client = state.get("client")
    if not isinstance(client, M3UndleClient):
        ctx.fail("ENC-01", str(state.get("reason")))
        return
    status, body = _get_status(client)
    valid = (
        status == 200
        and "encryptionAvailable" in body
        and "activeKeyId" in body
        and isinstance(body.get("knownKeyIds"), list)
        and "providersOnActiveKey" in body
        and "providersOnOtherKey" in body
    )
    ctx.record("ENC-01", valid, f"status={status} body={body}")


@SUITE.case("ENC-02")
def enc_02(ctx: Any, base_url: str, state: dict[str, object]) -> None:
    if _ready(ctx, state, "ENC-02") is None:
        return
    baseline = state.get("baseline_ring_entry")
    if not isinstance(baseline, str):
        ctx.skip("ENC-02", "Skipped because a baseline encryption ring was unavailable")
        return
    temporary_key = _generate_key()
    started, detail = _restart_with_env(
        base_url,
        {ENCRYPTION_KEYS_ENV: f"{ROTATION_KEY_ID}:{temporary_key},{baseline}"},
    )
    ctx.record("ENC-02", started, detail)
    if started:
        state["temporary_key"] = temporary_key
        state["rotation_ring_ready"] = True


@SUITE.case("ENC-03")
def enc_03(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "ENC-03")
    if client is None or not _prior_ok(ctx, state, "ENC-03", "rotation_ring_ready"):
        return
    status, body = _post_rotate(client)
    rotated = (
        status == 200
        and body.get("activeKeyId") == ROTATION_KEY_ID
        and int(body.get("providersMigrated") or 0) >= 1
    )
    ctx.record("ENC-03", rotated, f"status={status} body={body}")
    state["rotated"] = rotated


@SUITE.case("ENC-04")
def enc_04(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "ENC-04")
    if client is None or not _prior_ok(ctx, state, "ENC-04", "rotated"):
        return
    status, body = _get_status(client)
    settled = (
        status == 200
        and body.get("activeKeyId") == ROTATION_KEY_ID
        and int(body.get("providersOnOtherKey") or 0) == 0
    )
    ctx.record("ENC-04", settled, f"status={status} body={body}")
    state["settled"] = settled


@SUITE.case("ENC-05")
def enc_05(ctx: Any, base_url: str, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "ENC-05")
    if client is None or not _prior_ok(ctx, state, "ENC-05", "settled"):
        return
    temporary_key = state.get("temporary_key")
    provider_id = client.provider_id
    if not isinstance(temporary_key, str) or not provider_id:
        ctx.skip("ENC-05", "Skipped because the rotation key or provider identifier was unavailable")
        return
    started, detail = _restart_with_env(base_url, {ENCRYPTION_KEYS_ENV: f"{ROTATION_KEY_ID}:{temporary_key}"})
    if not started:
        ctx.record("ENC-05", False, f"Restart with old key removed did not become healthy: {detail}")
        return
    health_status, _ = client.get(f"/api/v1/providers/{provider_id}/health")
    valid = health_status == 200
    ctx.record("ENC-05", valid, f"Restarted with old key removed; provider health status={health_status}")
    state["old_key_removed"] = valid


@SUITE.case("ENC-06")
def enc_06(ctx: Any, state: dict[str, object]) -> None:
    client = _ready(ctx, state, "ENC-06")
    if client is None or not _prior_ok(ctx, state, "ENC-06", "old_key_removed"):
        return
    status, body = _post_rotate(client)
    valid = status == 200 and int(body.get("providersMigrated") or 0) == 0 and body.get("backupFileName") is None
    ctx.record("ENC-06", valid, f"status={status} body={body}")


@SUITE.teardown
def teardown(base_url: str, state: dict[str, object]) -> None:
    client = state.get("client")
    if isinstance(client, M3UndleClient) and client.provider_id:
        try:
            client.delete_provider(client.provider_id)
        except Exception:
            pass
    try:
        _unwind_rotation(base_url, state)
    except Exception:
        pass
    try:
        _restore_original_environment(base_url, state)
    except Exception:
        pass
    simulator = state.get("simulator")
    if isinstance(simulator, SimulatorInstance):
        try:
            simulator.stop()
        except Exception:
            pass
