"""Provider simulator lifecycle for the lab harness.

SimulatorInstance wraps provider_sim.py as either a managed local subprocess
or a managed Docker container. All generic subprocess/docker lifecycle, port
conflict cleanup, and health polling now live in se-lab's
agent.simulators.base.ExternalSimulator -- this module supplies only what's
actually specific to M3Undle's provider-simulator engine: its CLI contract
(--fixture/--public-host/--max-streams), the playlist_url convention, and
naming (env vars, image, container/label prefixes).

provider_sim.py itself is NOT lab code -- it's a separate, standalone public
product (Sydney-Elvis/M3Undle-provider-simulator, Docker + Compose only).
Point M3UNDLE_SIMULATOR_ENGINE_DIR in lab.env at a checkout of it.

Example:
    sim = SimulatorInstance(fixture="fixtures/providers/provider-a.json", port=19001)
    sim.start(log_path=Path("results/sim-19001.log"))
    if not sim.wait_healthy():
        raise RuntimeError("Simulator did not start")
    ...
    sim.stop()

Or as a context manager:
    with SimulatorInstance(...) as sim:
        sim.start()
        sim.wait_healthy()
        ...
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent import common as lab_common
from agent.simulators.base import ExternalSimulator

ENGINE_ENV_VAR = "M3UNDLE_SIMULATOR_ENGINE_DIR"

# Resolved eagerly (not required=True) because this module is imported
# unconditionally by m3undle_lab.commands, which every command (including
# ones that never touch the simulator) imports. Failing at import time here
# would break unrelated commands on hosts that never set this. Consumers
# that need the engine dir check for None themselves (see
# ExternalSimulator._require_engine_dir(), used lazily by start()).
_engine_dir_value = lab_common.resolve_setting(ENGINE_ENV_VAR)
SIMULATOR_ENGINE_DIR: Path | None = Path(_engine_dir_value) if _engine_dir_value else None


class SimulatorInstance(ExternalSimulator):
    engine_env_var = ENGINE_ENV_VAR
    backend_env_var = "M3UNDLE_SIMULATOR_BACKEND"
    image_env_var = "M3UNDLE_SIMULATOR_IMAGE"
    default_image = "m3undle-lab/provider-sim:dev"
    container_name_prefix = "m3undle-sim"
    docker_label_prefix = "com.m3undle-lab"
    process_marker = "provider_sim.py"
    reset_path = "/debug/reset"

    def __init__(
        self,
        *,
        fixture: str | Path,
        port: int = 19001,
        bind: str = "127.0.0.1",
        public_host: str | None = None,
        max_streams: int | None = None,
        backend: str | None = None,
        image: str | None = None,
        run_id: str | None = None,
        suite: str | None = None,
    ) -> None:
        """
        fixture     -- path to the provider fixture JSON this instance serves.
        max_streams -- optional concurrent-stream cap passed to the engine.

        See ExternalSimulator.__init__ for bind/public_host/backend/image/
        run_id/suite.
        """
        self.fixture = Path(fixture)
        self.max_streams = max_streams
        super().__init__(
            port=port, bind=bind, public_host=public_host, backend=backend, image=image, run_id=run_id, suite=suite,
        )
        self.playlist_url = f"{self.public_host}/playlist.m3u"

    def local_command(self, engine_dir: Path) -> list[str]:
        sim_script = engine_dir / "src" / "provider_sim.py"
        cmd = [
            sys.executable,
            str(sim_script),
            "--fixture", str(self.fixture),
            "--bind", self.bind,
            "--port", str(self.port),
            "--public-host", self.public_host,
        ]
        if self.max_streams is not None:
            cmd += ["--max-streams", str(self.max_streams)]
        return cmd

    def docker_run_args(self, image: str) -> list[str]:
        args = [
            "--bind", "0.0.0.0",
            "--port", str(self.port),
            "--fixture", self.container_path_for(self.fixture),
            "--public-host", self.public_host,
        ]
        if self.max_streams is not None:
            args += ["--max-streams", str(self.max_streams)]
        return args
