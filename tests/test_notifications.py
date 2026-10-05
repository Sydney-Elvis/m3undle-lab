"""Administrator notifications against real services, registered case-by-case with se-lab.

Real Matrix (Synapse) and SMTP (Mailpit) containers from docker-config/notifications.override.yaml, plus the in-process fault SMTP
server for what a catcher cannot produce. Every assertion is made from an independent observation: Mailpit's REST API for email and
a second Matrix account for the room. The suite is optional and does not change the default lab topology.

Run (see README "Notifications suite"): ./lab run --only notifications [--case NOTIF-01]
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from agent import common as lab_common
from agent.container import wait_up
from agent.suites import suite

from m3undle_lab import notification_cases as cases
from m3undle_lab import notifications as fixtures
from m3undle_lab.commands import CONTAINER_NAME, HOST_OVERRIDE


SUITE = suite("notifications", group="notifications", order=210)
NOTIFICATIONS_OVERRIDE = Path(__file__).resolve().parents[1] / "docker-config" / "notifications.override.yaml"
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "notifications"
MATRIX_CONTAINER = "m3undle-lab-notif-matrix"
SMTP_CONTAINER = "m3undle-lab-notif-smtp"
EXTRA_FILES = [HOST_OVERRIDE, NOTIFICATIONS_OVERRIDE]
LOOPBACK = "127.0.0.1"
CRL_PORT = 18099
SMTP_PORT = 2525
MAILPIT_API = f"http://{LOOPBACK}:8025"
MATRIX_URL = f"http://{LOOPBACK}:8008"


class _CaseRecorder:
    """Collapses a case's independent checks into the single result se-lab records per case ID."""

    def __init__(self, ctx: Any, case_id: str) -> None:
        self._ctx = ctx
        self._case_id = case_id
        self._results: list[tuple[str, bool, str]] = []
        self._terminal = False

    def record(self, test_id: str, passed: bool, detail: str) -> None:
        self._results.append((test_id, passed, detail))

    def fail(self, test_id: str, detail: str) -> None:
        self._terminal = True
        self._ctx.fail(self._case_id, detail)

    def skip(self, test_id: str, reason: str) -> None:
        self._terminal = True
        self._ctx.skip(self._case_id, reason)

    def finish(self) -> None:
        if self._terminal:
            return
        if not self._results:
            self._ctx.fail(self._case_id, "the case recorded no checks")
            return
        passed = all(ok for _, ok, _ in self._results)
        detail = "; ".join(f"{'PASS' if ok else 'FAIL'} {tid}: {text}" for tid, ok, text in self._results)
        self._ctx.record(self._case_id, passed, detail)


def _docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True)


def _compose_up(base_url: str) -> bool:
    os.environ["COMPOSE_PROFILES"] = "notifications"
    lab_common.compose_up(extra_compose_files=EXTRA_FILES)
    return wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health"))


@SUITE.setup
def setup(base_url: str) -> dict[str, Any]:
    runtime = lab_common.runtime_dir() / "notifications"
    certs = fixtures.ensure_test_certificates(runtime / "ca", crl_url=f"http://{LOOPBACK}:{CRL_PORT}/ca.crl")
    crl_server = fixtures.serve_crl(certs, bind=LOOPBACK, port=CRL_PORT)
    (runtime / "ca" / "smtp-auth.txt").write_text((FIXTURES_DIR / "smtp-auth.txt").read_text())
    fixtures.prepare_synapse_config(runtime / "synapse", bind=LOOPBACK)

    if not _compose_up(base_url):
        raise RuntimeError("M3Undle did not become healthy with the notification fixtures attached")

    mail = fixtures.MailpitClient(MAILPIT_API)
    fixtures.wait_for(lambda: fixtures.http_json("GET", f"{MAILPIT_API}/api/v1/info", timeout=5)[0] == 200, timeout=60, what="Mailpit")
    matrix = fixtures.MatrixFixture(MATRIX_URL)
    matrix.wait_ready()
    matrix.provision()

    epg = cases.EpgToggleServer()
    epg.start()

    def restart_m3undle() -> None:
        _docker("restart", CONTAINER_NAME)

    scenario = cases.Scenario(
        api=cases.NotificationsApi(base_url), mail=mail, matrix=matrix, certs=certs,
        epg_url_for_m3undle=f"http://{LOOPBACK}:{epg.port}/epg.xml", epg=epg,
        smtp_host=LOOPBACK, smtp_port=SMTP_PORT, matrix_url=MATRIX_URL, fault_host=LOOPBACK,
        restart_m3undle=restart_m3undle,
        stop_service=lambda name: _docker("stop", MATRIX_CONTAINER if name == "matrix" else SMTP_CONTAINER),
        start_service=lambda name: _docker("start", MATRIX_CONTAINER if name == "matrix" else SMTP_CONTAINER),
        wait_m3undle=lambda: wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health")) or (_ for _ in ()).throw(RuntimeError("M3Undle did not return healthy")),
        base_url=base_url,
    )
    scenario.state["crl_server"] = crl_server
    return {"scenario": scenario}


def _run(ctx: Any, case_id: str, scenario: cases.Scenario, *extra: Any) -> None:
    recorder = _CaseRecorder(ctx, case_id)
    func = dict(cases.CASES)[case_id]
    try:
        func(recorder, scenario, *extra)
    except Exception as error:  # a fixture or timeout problem is a failed case, not a crashed suite
        recorder.fail(case_id, f"{type(error).__name__}: {error}")
        return
    recorder.finish()


@SUITE.case("NOTIF-01")
def notif_01(ctx, base_url: str, scenario: cases.Scenario) -> None:
    _run(ctx, "NOTIF-01", scenario)


@SUITE.case("NOTIF-02")
def notif_02(ctx, base_url: str, scenario: cases.Scenario) -> None:
    _run(ctx, "NOTIF-02", scenario)


@SUITE.case("NOTIF-03")
def notif_03(ctx, base_url: str, scenario: cases.Scenario) -> None:
    _run(ctx, "NOTIF-03", scenario, scenario.certs)


@SUITE.case("NOTIF-04")
def notif_04(ctx, base_url: str, scenario: cases.Scenario) -> None:
    _run(ctx, "NOTIF-04", scenario, scenario.certs)


@SUITE.case("NOTIF-05")
def notif_05(ctx, base_url: str, scenario: cases.Scenario) -> None:
    _run(ctx, "NOTIF-05", scenario)


@SUITE.case("NOTIF-06")
def notif_06(ctx, base_url: str, scenario: cases.Scenario) -> None:
    _run(ctx, "NOTIF-06", scenario)


@SUITE.case("NOTIF-07")
def notif_07(ctx, base_url: str, scenario: cases.Scenario) -> None:
    _run(ctx, "NOTIF-07", scenario)


@SUITE.case("NOTIF-08")
def notif_08(ctx, base_url: str, scenario: cases.Scenario) -> None:
    _run(ctx, "NOTIF-08", scenario)


@SUITE.teardown
def teardown(base_url: str, scenario: cases.Scenario) -> None:
    scenario.epg.stop()
    try:
        scenario.remove_stale_sources()
    except Exception:
        pass
    crl_server = scenario.state.get("crl_server")
    if crl_server is not None:
        crl_server.shutdown()
        crl_server.server_close()
    try:
        scenario.api.all_off()
    except Exception:
        pass
    for name in (MATRIX_CONTAINER, SMTP_CONTAINER):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    os.environ.pop("COMPOSE_PROFILES", None)
    # Back to the default topology: no fixtures, no CA mount, no lab-only environment.
    lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
    wait_up(base_url, CONTAINER_NAME, health_paths=("/livez", "/health"))
