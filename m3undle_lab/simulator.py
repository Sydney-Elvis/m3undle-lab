"""
Provider simulator lifecycle for the lab harness.

Ported from m3undle-lab-private's scripts/common/harness/simulator.py (576
lines), adapted only at the import layer -- everything else (process/docker
lifecycle, port cleanup, health polling) is self-contained and needed no
redesign. FIXTURES_DIR/LAB_ROOT come from se-lab's agent.runtime.REPO_ROOT
now instead of the old lab's own common.py; resolve_setting() is se-lab's.

SimulatorInstance wraps provider_sim.py as either a managed local subprocess
or a managed Docker container. It supports context-manager usage so teardown
is guaranteed even if a test script exits early.

provider_sim.py itself is NOT lab code -- it's a separate, standalone public
product (Sydney-Elvis/M3Undle-provider-simulator, Docker + Compose only).
Point M3UNDLE_SIMULATOR_ENGINE_DIR in lab.env at a checkout of it.

Backend selection is centralized so callers don't need separate local and
Docker code paths. Resolution order: explicit `backend=` argument, then the
`M3UNDLE_SIMULATOR_BACKEND` env var / lab.env setting, then "local".

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

import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from agent import common as lab_common, runtime

LAB_ROOT = runtime.REPO_ROOT
FIXTURES_DIR = LAB_ROOT / "fixtures"
# The lab's own private scenarios/ (cooldown-*, etc.), unrelated to the
# engine's own baked-in example scenarios under the engine checkout's own
# scenarios/core/.
SCENARIOS_DIR = LAB_ROOT / "scenarios"


def _engine_dir_or_none() -> Path | None:
    # Deliberately not `required=True`: this module is imported unconditionally
    # by m3undle_lab.commands, which every command (including ones that never
    # touch the simulator) imports. Failing at import time here would break
    # unrelated commands on hosts that never set this. Instead, resolve
    # lazily to None and raise only where the simulator is actually started
    # -- see _require_engine_dir().
    value = lab_common.resolve_setting("M3UNDLE_SIMULATOR_ENGINE_DIR")
    return Path(value) if value else None


def _require_engine_dir() -> Path:
    if SIMULATOR_ENGINE_DIR is None:
        raise RuntimeError(
            "M3UNDLE_SIMULATOR_ENGINE_DIR is not set. Point it at a checkout of "
            "the public provider-simulator engine "
            "(Sydney-Elvis/M3Undle-provider-simulator) in lab.env."
        )
    return SIMULATOR_ENGINE_DIR


SIMULATOR_ENGINE_DIR = _engine_dir_or_none()

VALID_BACKENDS = frozenset({"local", "docker"})
DEFAULT_SIMULATOR_IMAGE = "m3undle-lab/provider-sim:dev"
SIMULATOR_IMAGE_ENV_VAR = "M3UNDLE_SIMULATOR_IMAGE"


class DockerUnavailableError(RuntimeError):
    """Raised when the docker backend is selected but the CLI can't run."""


def _run_docker(*args: str, timeout: float | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=check,
        )
    except FileNotFoundError as exc:
        raise DockerUnavailableError("Docker backend requested but the `docker` CLI is not available") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"docker {' '.join(args)} failed: {exc.stderr.strip()}") from exc


def build_default_simulator_image() -> str:
    """Build DEFAULT_SIMULATOR_IMAGE from the current source and return its tag.

    Exposed so a multi-suite runner (`lab run --simulator docker`) can build
    once per invocation and export the result via SIMULATOR_IMAGE_ENV_VAR,
    instead of every SimulatorInstance.start() repeating the same build.
    """
    engine_dir = _require_engine_dir()
    dockerfile = engine_dir / "Dockerfile"
    print(f"Building simulator image {DEFAULT_SIMULATOR_IMAGE} from {dockerfile}...", flush=True)
    _run_docker(
        "build",
        "-f", str(dockerfile),
        "-t", DEFAULT_SIMULATOR_IMAGE,
        str(engine_dir),
    )
    return DEFAULT_SIMULATOR_IMAGE


class SimulatorInstance:
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
        bind        — interface the simulator listens on (default 127.0.0.1).
                      Ignored by the docker backend, which always binds
                      0.0.0.0 inside the container and reaches the host via
                      the published port instead.
        public_host — base URL advertised in the M3U and used as the provider
                      playlist URL.  Defaults to http://{bind}:{port}.

                      When M3Undle runs inside Docker, set this to
                      http://host.docker.internal:{port} (or the lab's own
                      Docker gateway IP, see agent.container.get_docker_gateway())
                      so the container can reach the simulator on the host.
                      The bind should be "0.0.0.0" in that case so the host
                      interface is open.
        backend     — "local" (subprocess) or "docker" (container). Defaults
                      to the M3UNDLE_SIMULATOR_BACKEND env var / lab.env
                      setting, then "local". Callers should not hard-code
                      this — let it resolve centrally so the same test code
                      runs against either backend.
        image       — docker backend only. Defaults to
                      DEFAULT_SIMULATOR_IMAGE, which is rebuilt from the
                      external engine checkout's root Dockerfile
                      ($M3UNDLE_SIMULATOR_ENGINE_DIR/Dockerfile) on every
                      start() so the container always reflects current
                      source. Set this to a pinned/pulled tag to skip that
                      rebuild — or leave it unset and export
                      M3UNDLE_SIMULATOR_IMAGE: it builds the image once per
                      run and passes the tag down via that env var, so every
                      suite's SimulatorInstance skips the redundant rebuild
                      instead of each one repeating it.
        run_id, suite — optional; recorded as container labels
                      (com.m3undle-lab.run-id / .suite) for the docker
                      backend. Not required.
        """
        self.fixture = Path(fixture)
        self.port = port
        self.bind = bind
        self.public_host = public_host or f"http://{bind}:{port}"
        self.max_streams = max_streams
        self.playlist_url = f"{self.public_host}/playlist.m3u"

        self.backend = backend or lab_common.resolve_setting("M3UNDLE_SIMULATOR_BACKEND", default="local")
        if self.backend not in VALID_BACKENDS:
            raise ValueError(
                f"Unsupported simulator backend {self.backend!r}. Expected one of: {', '.join(sorted(VALID_BACKENDS))}."
            )
        self.image = image or os.environ.get(SIMULATOR_IMAGE_ENV_VAR) or DEFAULT_SIMULATOR_IMAGE
        self._image_is_default = image is None and not os.environ.get(SIMULATOR_IMAGE_ENV_VAR)
        self.run_id = run_id
        self.suite = suite
        self.container_name = f"m3undle-sim-{port}"

        # Local health-check URL — always reachable from the host regardless
        # of what public_host is set to (e.g. host.docker.internal won't
        # resolve on the host itself), and identical for both backends since
        # the docker backend always publishes the container port to the host.
        _local_bind = "127.0.0.1" if bind == "0.0.0.0" else bind
        self._local_url = f"http://{_local_bind}:{port}"
        self._process: subprocess.Popen | None = None
        self._log_fh = None
        self._log_path: Path | None = None
        self._container_started = False

    # -----------------------------------------------------------------------
    # Port/process helpers (local backend)
    # -----------------------------------------------------------------------

    def _port_is_bindable(self) -> bool:
        """Return True when the target port can be bound on the configured interface."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((self.bind, self.port))
            return True
        except OSError:
            return False

    def _list_listening_pids(self) -> list[int]:
        """Best-effort PID discovery for listeners on the simulator port."""
        pids: set[int] = set()

        # Preferred path on Linux: parse `ss -ltnp` output.
        try:
            result = subprocess.run(
                ["ss", "-ltnp"],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if f":{self.port}" not in line:
                        continue
                    for match in re.finditer(r"pid=(\d+)", line):
                        pids.add(int(match.group(1)))
        except FileNotFoundError:
            pass

        # Fallback path when `ss` is unavailable.
        if not pids:
            try:
                result = subprocess.run(
                    ["lsof", "-nP", f"-iTCP:{self.port}", "-sTCP:LISTEN", "-t"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if result.returncode == 0:
                    for raw in result.stdout.splitlines():
                        raw = raw.strip()
                        if raw.isdigit():
                            pids.add(int(raw))
            except FileNotFoundError:
                pass

        return sorted(pid for pid in pids if pid > 0 and pid != os.getpid())

    @staticmethod
    def _cmdline_for_pid(pid: int) -> str:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return ""
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()

    @staticmethod
    def _terminate_pid(pid: int, *, timeout: float = 5.0) -> bool:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                break
            time.sleep(0.1)

        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return True

    def _cleanup_stale_port_owner(self) -> None:
        """
        Best-effort cleanup:
        - Kill stale provider_sim.py listeners on this port.
        - Refuse startup if a non-simulator process still owns the port.
        """
        if self._port_is_bindable():
            return

        pids = self._list_listening_pids()
        if not pids:
            # Port is occupied but owner could not be identified; let startup
            # proceed so bind failure is logged with a clear traceback.
            return

        stale_sim_pids: list[int] = []
        foreign_pids: list[int] = []
        for pid in pids:
            cmdline = self._cmdline_for_pid(pid)
            if "provider_sim.py" in cmdline:
                stale_sim_pids.append(pid)
            else:
                foreign_pids.append(pid)

        if stale_sim_pids:
            print(
                f"  Port {self.port} already in use by stale simulator PID(s): {stale_sim_pids}. "
                "Attempting cleanup...",
                flush=True,
            )
            for pid in stale_sim_pids:
                self._terminate_pid(pid)

            time.sleep(0.2)

        if self._port_is_bindable():
            return

        remaining = self._list_listening_pids()
        if not remaining:
            return

        remaining_cmd = ", ".join(
            f"{pid}:{self._cmdline_for_pid(pid) or '<unknown>'}" for pid in remaining
        )
        if foreign_pids:
            raise RuntimeError(
                f"Port {self.port} is occupied by non-simulator process(es): {remaining_cmd}"
            )
        raise RuntimeError(
            f"Port {self.port} still occupied after stale simulator cleanup: {remaining_cmd}"
        )

    # -----------------------------------------------------------------------
    # Docker helpers
    # -----------------------------------------------------------------------

    def _ensure_image(self) -> None:
        if not self._image_is_default:
            # Caller pinned an explicit image (e.g. a pulled/published tag,
            # or SIMULATOR_IMAGE_ENV_VAR signaling a run-level prebuild);
            # treat it as externally managed and never rebuild it.
            return
        build_default_simulator_image()

    def _container_fixture_arg(self) -> str:
        host_fixture = self.fixture.resolve()
        for host_root, container_root in (
            (FIXTURES_DIR.resolve(), "/app/fixtures"),
            (SCENARIOS_DIR.resolve(), "/app/scenarios"),
        ):
            try:
                rel = host_fixture.relative_to(host_root)
            except ValueError:
                continue
            return f"{container_root}/{rel.as_posix()}"
        raise RuntimeError(
            f"Docker backend requires fixtures/scenarios under {FIXTURES_DIR} or "
            f"{SCENARIOS_DIR} (got {host_fixture}); those directories are what get "
            "bind-mounted into the container."
        )

    def _cleanup_stale_container(self) -> None:
        _run_docker("rm", "-f", self.container_name, check=False)

    def _start_docker(self, log_path: Path | None) -> None:
        self._ensure_image()
        self._cleanup_stale_container()

        cmd = [
            "run", "-d",
            "--name", self.container_name,
            "-p", f"{self.port}:{self.port}",
            "-v", f"{FIXTURES_DIR.resolve()}:/app/fixtures:ro",
        ]
        if SCENARIOS_DIR.is_dir():
            cmd += ["-v", f"{SCENARIOS_DIR.resolve()}:/app/scenarios:ro"]
        cmd += [
            "--label", "com.m3undle-lab.component=provider-sim",
            "--label", f"com.m3undle-lab.port={self.port}",
        ]
        if self.run_id:
            cmd += ["--label", f"com.m3undle-lab.run-id={self.run_id}"]
        if self.suite:
            cmd += ["--label", f"com.m3undle-lab.suite={self.suite}"]
        cmd += [
            self.image,
            "--bind", "0.0.0.0",
            "--port", str(self.port),
            "--fixture", self._container_fixture_arg(),
            "--public-host", self.public_host,
        ]
        if self.max_streams is not None:
            cmd += ["--max-streams", str(self.max_streams)]

        print(f"  Starting simulator container {self.container_name} on port {self.port}...", flush=True)
        _run_docker(*cmd)
        self._container_started = True
        self._log_path = log_path

    def _stop_docker(self) -> None:
        if not self._container_started:
            return
        print(f"  Stopping simulator container {self.container_name}...", flush=True)
        if self._log_path is not None:
            try:
                logs = _run_docker("logs", self.container_name, check=False)
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_path.write_text(logs.stdout + logs.stderr, encoding="utf-8")
            except DockerUnavailableError:
                pass
        _run_docker("rm", "-f", self.container_name, check=False)
        self._container_started = False

    def _docker_is_running(self) -> bool:
        if not self._container_started:
            return False
        try:
            result = _run_docker(
                "inspect", "--format", "{{.State.Running}}", self.container_name, check=False
            )
        except DockerUnavailableError:
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def start(self, log_path: Path | None = None) -> None:
        if self.is_running():
            raise RuntimeError(f"Simulator on port {self.port} is already running")

        self._cleanup_stale_port_owner()

        if self.backend == "docker":
            self._start_docker(log_path)
            return

        engine_dir = _require_engine_dir()
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
            cmd.extend(["--max-streams", str(self.max_streams)])

        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = open(log_path, "w", encoding="utf-8")
            stdout = stderr = self._log_fh
        else:
            stdout = stderr = subprocess.DEVNULL

        print(
            f"  Starting simulator on port {self.port}"
            f" (fixture: {self.fixture.name})...",
            flush=True,
        )
        # cwd pinned to LAB_ROOT: lab-private fixtures (e.g. payload_file:
        # "fixtures/media/...") live outside the external engine checkout,
        # so the engine's own fixture resolver only finds them via its
        # cwd-relative fallback candidate -- that must not depend on where
        # `./lab` itself was invoked from. The docker backend takes a different
        # path -- it mounts LAB_ROOT/fixtures into the container directly and
        # doesn't go through this method.
        self._process = subprocess.Popen(
            cmd,
            cwd=str(LAB_ROOT),
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )

    def wait_healthy(self, *, timeout: float = 15.0) -> bool:
        """Poll /health until the simulator responds 200 or timeout elapses."""
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            # Fail fast if the process/container has already exited.
            if not self.is_running():
                print("  Simulator exited early before becoming healthy", flush=True)
                return False
            try:
                with urllib.request.urlopen(
                    f"{self._local_url}/health", timeout=2.0
                ) as resp:
                    if resp.status == 200:
                        return True
                    last_error = f"HTTP {resp.status}"
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.5)
        print(f"  Simulator health check timed out. Last error: {last_error}", flush=True)
        return False

    def stop(self) -> None:
        if self.backend == "docker":
            self._stop_docker()
            return

        if self._process is None:
            return
        print(f"  Stopping simulator (port {self.port})...", flush=True)

        proc = self._process
        pid = proc.pid
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            # Fallback for environments where process group signaling is
            # unavailable.
            proc.terminate()

        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                proc.kill()
            proc.wait()
        finally:
            self._process = None

        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None

    # -----------------------------------------------------------------------
    # Debug helpers
    # -----------------------------------------------------------------------

    def reset(self) -> None:
        """Clear simulator metrics and event log via POST /debug/reset."""
        try:
            req = urllib.request.Request(
                f"{self._local_url}/debug/reset", method="POST"
            )
            urllib.request.urlopen(req, timeout=5.0)
        except Exception:
            pass

    def is_running(self) -> bool:
        if self.backend == "docker":
            return self._docker_is_running()
        return self._process is not None and self._process.poll() is None

    # -----------------------------------------------------------------------
    # Context manager
    # -----------------------------------------------------------------------

    def __enter__(self) -> "SimulatorInstance":
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
