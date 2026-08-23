"""Proves the lab can consume provider-simulator's --run/--result-file
self-verification artifact (the simulator engine's own exit-gate item: "the
lab can consume the result artifact in at least one suite").

Additive and self-contained: it does not touch or replace any of the
run_stream_*_scenarios.py-style runners, and it needs no M3Undle instance at
all -- --run mode is a self-contained engine invocation with its own
built-in driven client, so there is nothing here for SimulatorInstance/Docker
to manage. The engine is invoked directly via subprocess, not imported.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from agent import common as lab_common
from agent.suites import suite

from m3undle_lab.simulator import SIMULATOR_ENGINE_DIR

SUITE = suite("simulator-run-mode", group="core", order=15)

# Picked for being fast -- mirrors the engine's own tests/test_run_mode.py
# RunModeRealScenarioTests selection, so this suite stays within the usual
# per-suite time budget.
SCENARIOS = [
    "baseline-05-abrupt-connection-close.yaml",
    "baseline-08-temporary-http-503.yaml",
    "baseline-10-malformed-ts-packet-sequence.yaml",
]


def _run_scenario(scenario_name: str) -> dict[str, Any]:
    if SIMULATOR_ENGINE_DIR is None:  # narrows for mypy/type-checkers; _setup already guarantees this
        raise RuntimeError("M3UNDLE_SIMULATOR_ENGINE_DIR is not set.")

    sim_script = SIMULATOR_ENGINE_DIR / "src" / "provider_sim.py"
    scenario_path = SIMULATOR_ENGINE_DIR / "scenarios" / "core" / scenario_name
    label = scenario_name.removesuffix(".yaml")

    with tempfile.TemporaryDirectory() as tmp:
        result_file = Path(tmp) / f"{scenario_name}.result.json"
        proc = subprocess.run(
            [
                sys.executable, str(sim_script),
                "--run", str(scenario_path),
                "--result-file", str(result_file),
                "--settle-seconds", "0.3",
            ],
            cwd=str(SIMULATOR_ENGINE_DIR),
            text=True,
            capture_output=True,
            timeout=90,
        )

        if not result_file.exists():
            return {
                "artifact_written": False,
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            }

        result = json.loads(result_file.read_text(encoding="utf-8"))
        (lab_common.runtime_results_dir() / f"simulator-run-mode-{label}.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        result["artifact_written"] = True
        return result


@SUITE.setup
def _setup() -> dict[str, Any]:
    if SIMULATOR_ENGINE_DIR is None:
        raise RuntimeError(
            "M3UNDLE_SIMULATOR_ENGINE_DIR is not set. Point it at a checkout of "
            "the public provider-simulator engine "
            "(Sydney-Elvis/M3Undle-provider-simulator) in lab.env."
        )
    return {"scenario_results": {name: _run_scenario(name) for name in SCENARIOS}}


def _register_scenario_cases(scenario_name: str) -> None:
    label = scenario_name.removesuffix(".yaml")

    @SUITE.case(f"RUN-MODE-{label}-passed")
    def _passed(ctx, scenario_results):
        result = scenario_results[scenario_name]
        test_id = f"RUN-MODE-{label}-passed"
        if not result["artifact_written"]:
            ctx.fail(test_id, f"--result-file was not written; exit={result['exit_code']}", result)
            return
        ctx.record(
            test_id,
            result.get("passed") is True,
            f"engine exit={result.get('exit_code')}, passed={result.get('passed')}, "
            f"failure_reason={result.get('failure_reason')}",
        )

    @SUITE.case(f"RUN-MODE-{label}-not-aborted")
    def _not_aborted(ctx, scenario_results):
        result = scenario_results[scenario_name]
        test_id = f"RUN-MODE-{label}-not-aborted"
        if not result["artifact_written"]:
            ctx.fail(test_id, f"--result-file was not written; exit={result['exit_code']}", result)
            return
        ctx.record(
            test_id,
            result.get("sequence_aborted") is False,
            f"sequence_aborted={result.get('sequence_aborted')}",
        )

    @SUITE.case(f"RUN-MODE-{label}-expected-events-matched")
    def _events_matched(ctx, scenario_results):
        result = scenario_results[scenario_name]
        test_id = f"RUN-MODE-{label}-expected-events-matched"
        if not result["artifact_written"]:
            ctx.fail(test_id, f"--result-file was not written; exit={result['exit_code']}", result)
            return
        expected_events = result.get("expected_events", [])
        all_matched = bool(expected_events) and all(e.get("matched") for e in expected_events)
        ctx.record(test_id, all_matched, f"expected_events={expected_events}")


for _scenario_name in SCENARIOS:
    _register_scenario_cases(_scenario_name)
