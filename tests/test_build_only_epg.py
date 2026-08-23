"""Port of the frozen build-only EPG recompilation regression suite."""

from __future__ import annotations

import platform
import time
import urllib.request
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from agent.container import get_docker_gateway
from agent.suites import suite

from m3undle_lab.api import M3UndleClient
from m3undle_lab.simulator import SimulatorInstance


SUITE = suite("build-epg", group="core", order=130)
SIM_PORT = 19018
SIM_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "provider-epg.json"


def _simulator_address() -> tuple[str, str | None]:
    """Return the listener and advertised URL suitable for this Docker host."""
    if platform.system() == "Darwin":
        return "0.0.0.0", f"http://host.docker.internal:{SIM_PORT}"

    gateway = get_docker_gateway("m3undle-lab_media")
    if gateway:
        return "0.0.0.0", f"http://{gateway}:{SIM_PORT}"
    return "127.0.0.1", None


def _programmes(base_url: str) -> list[ElementTree.Element]:
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/xmltv/m3undle.xml", timeout=15.0) as response:
        root = ElementTree.fromstring(response.read())
    return root.findall(".//programme") or root.findall(".//{*}programme")


@SUITE.setup
def setup(base_url: str) -> dict[str, object]:
    state: dict[str, object] = {
        "ready": False,
        "reason": "Build-only EPG suite setup did not complete",
        "client": None,
        "simulator": None,
        "epg_url": "",
    }
    try:
        bind, public_host = _simulator_address()
        simulator = SimulatorInstance(
            fixture=SIM_FIXTURE,
            port=SIM_PORT,
            bind=bind,
            public_host=public_host,
            suite="build-epg",
        )
        state["simulator"] = simulator
        simulator.start()
        if not simulator.wait_healthy():
            state["reason"] = "Provider simulator did not become healthy"
            return {"state": state}

        client = M3UndleClient(base_url)
        state["client"] = client
        if not client.setup(playlist_url=simulator.playlist_url, provider_name="build-epg-sim"):
            state["reason"] = client.last_setup_error or "M3Undle provider setup failed"
            return {"state": state}

        state["epg_url"] = f"{simulator.public_host}/epg/guide.xml"
        state["ready"] = True
        return {"state": state}
    except Exception as exc:  # setup errors are reported as a case skip below
        state["reason"] = f"Build-only EPG suite setup failed: {exc}"
        return {"state": state}


def _ready(ctx: Any, state: dict[str, object], test_id: str) -> tuple[M3UndleClient, str] | None:
    client = state.get("client")
    epg_url = state.get("epg_url")
    if state.get("ready") and isinstance(client, M3UndleClient) and isinstance(epg_url, str) and epg_url:
        return client, epg_url
    ctx.skip(test_id, str(state.get("reason") or "Build-only EPG suite setup did not complete"))
    return None


@SUITE.case("BUILD-EPG-01")
def build_epg_01(ctx: Any, base_url: str, state: dict[str, object]) -> None:
    ready = _ready(ctx, state, "BUILD-EPG-01")
    if ready is None:
        return
    client, epg_url = ready
    profile_id = str(client.profile_id or "")
    provider_id = str(client.provider_id or "")
    if not profile_id or not provider_id:
        ctx.fail("BUILD-EPG-01", "No active profile or provider after setup")
        return

    try:
        initial_programmes = _programmes(base_url)
    except Exception as exc:
        ctx.fail("BUILD-EPG-01", f"Initial XMLTV fetch failed: {exc}")
        return
    if initial_programmes:
        ctx.skip(
            "BUILD-EPG-01",
            f"Initial guide already has {len(initial_programmes)} programme(s); provider may have a url-tvg header",
        )
        return

    status, body = client.post(
        "/api/v1/epg/sources",
        {
            "providerId": provider_id,
            "name": f"sim-epg-{int(time.time())}",
            "kind": "xmltv_url",
            "urlOrPath": epg_url,
            "enabled": True,
            "priority": 10,
        },
    )
    if status not in (200, 201) or not isinstance(body, dict):
        ctx.fail("BUILD-EPG-01", f"EPG source creation failed: status={status} body={body!r}")
        return
    source_id = str(body.get("epgSourceId") or body.get("sourceId") or "")
    if not source_id:
        ctx.fail("BUILD-EPG-01", f"EPG source ID missing from response: {body}")
        return

    try:
        client.build_snapshot()
    except RuntimeError as exc:
        ctx.fail("BUILD-EPG-01", f"build_snapshot failed: {exc}")
        return
    if not client.poll_build_completion(profile_id, timeout_seconds=120.0):
        ctx.fail("BUILD-EPG-01", "Build did not complete within timeout")
        return

    try:
        programmes = _programmes(base_url)
    except Exception as exc:
        ctx.fail("BUILD-EPG-01", f"XMLTV fetch after build failed: {exc}")
        return
    ctx.record(
        "BUILD-EPG-01",
        bool(programmes),
        f"programmes_after_build={len(programmes)} (initial=0, source={source_id!r})",
    )


@SUITE.teardown
def teardown(state: dict[str, object]) -> None:
    client = state.get("client")
    if isinstance(client, M3UndleClient):
        try:
            client.clear_existing_providers()
        except Exception:
            pass

    simulator = state.get("simulator")
    if isinstance(simulator, SimulatorInstance):
        try:
            simulator.stop()
        except Exception:
            pass
