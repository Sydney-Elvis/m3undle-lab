"""M3Undle lifecycle commands built on se-lab's generic primitives."""

from __future__ import annotations

import argparse
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from agent import common as lab_common, registry
from agent.container import wait_up
from agent.planning import RunPlan
from agent.status import BaseStatus
from agent.suites import discover_suites, run_suites, select_suites

from .analysis import M3UndleAnalysis
from .database import M3UndleDatabase
from .settings import M3UndleSettings
from . import clients as _clients  # noqa: F401 - class decorators register product clients


DEFAULT_REPO_URL = "https://github.com/Sydney-Elvis/M3Undle.git"
DEFAULT_GHCR_IMAGE = "ghcr.io/sydney-elvis/m3undle"
SERVICE = "m3undle"
CONTAINER_NAME = "m3undle-lab"
HOST_OVERRIDE = Path(__file__).resolve().parents[1] / "docker-config" / "m3undle-host-network.override.yaml"
TESTS_DIR = Path(__file__).resolve().parents[1] / "tests"
CHECKLIST_TEMPLATE = Path(__file__).resolve().parents[1] / "docs" / "checklist-template.md"

registry.set_analysis_plugin(M3UndleAnalysis())
registry.set_database_plugin(M3UndleDatabase())
registry.set_settings_plugin(M3UndleSettings())
# Host-network mode (automated suites) and the bridge-network override (clients
# up) both publish these to the host -- see docker-config/m3undle-bridge-network
# .override.yaml. Docker can't report a bind conflict for host-network mode
# itself, hence se-lab's preflight check needing to know them explicitly.
registry.set_required_host_ports((5004, 8080, 9999))


def _layout() -> None:
    """Create M3Undle's product directories and project lab.env into runtime .env."""
    runtime = lab_common.runtime_dir()
    (runtime / "m3u_data").mkdir(parents=True, exist_ok=True)
    for sub in ("jellyfin/config", "jellyfin/cache", "nextpvr/config", "nextpvr/recordings", "nextpvr/buffer"):
        (runtime / "client-apps" / sub).mkdir(parents=True, exist_ok=True)
    values = lab_common.load_lab_env()
    product_values = {name: value for name, value in values.items() if name.startswith("M3UNDLE_")}
    if product_values:
        # This hook runs from common.ensure_layout(), so calling
        # set_runtime_env_values() here would recurse back into this hook.
        lab_common.write_env_file_values(lab_common.runtime_env_file(), product_values)


registry.set_layout_hook(_layout)


def _base_url() -> str:
    return lab_common.resolve_setting("M3UNDLE_BASE_URL", default="http://127.0.0.1:8080") or "http://127.0.0.1:8080"


def _repo_url() -> str:
    """Plugin-level default: this plugin only ever tests M3Undle, so it can just
    know that -- M3UNDLE_REPO_URL in lab.env becomes an optional fork/mirror
    override rather than a required setting."""
    return lab_common.resolve_setting("M3UNDLE_REPO_URL", default=DEFAULT_REPO_URL) or DEFAULT_REPO_URL


def _ghcr_image() -> str:
    return lab_common.resolve_setting("M3UNDLE_GHCR_IMAGE", default=DEFAULT_GHCR_IMAGE) or DEFAULT_GHCR_IMAGE


def _wait_healthy() -> None:
    if not wait_up(_base_url(), CONTAINER_NAME, health_paths=("/livez", "/health")):
        raise SystemExit("M3Undle did not become healthy. Run './lab status --logs 100' for diagnostics.")


def _host_compose_up() -> None:
    lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
    _wait_healthy()


def _build_and_deploy(target: str | None, *, local: bool) -> str:
    """Build (from source or the cached local checkout) and deploy, via
    se-lab's generic resolve_build_target()/deploy_branch()/deploy_source_tag()
    -- both already call compose_up() internally, so this function's callers
    only need to add M3Undle's own health-check afterward, not a second
    compose_up.
    """
    repo_url = _repo_url()
    if local:
        lab_common.ensure_repo_checkout(repo_url)
        branch = lab_common.repo_current_branch() or "local"
        image = lab_common.local_branch_image(branch)
        commit = lab_common.repo_head_commit()
        lab_common.docker_build(image, lab_common.repo_dir(), source_revision=commit)
        lab_common.sync_runtime_compose()
        lab_common.set_deployment_metadata("local", branch, image=image, source_commit=commit)
        lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
        return image
    assert target is not None
    kind, ref = lab_common.resolve_build_target(target, repo_url)
    if kind == "tag":
        return lab_common.deploy_source_tag(ref, repo_url, extra_compose_files=[HOST_OVERRIDE])
    return lab_common.deploy_branch(ref, repo_url, extra_compose_files=[HOST_OVERRIDE])


def _pull_and_deploy(tag: str) -> str:
    """Pull the actual published GHCR image for a release tag, rather than building
    from source at that tag's commit (what `build`/`up`/`run <tag>` do) -- the two
    can diverge (a platform-specific build issue, a broken publish step, drift
    between what got tagged and what CI actually built), so this is a distinct
    verb, not an alternate path to the same result.
    """
    return lab_common.deploy_tag(tag, _ghcr_image(), extra_compose_files=[HOST_OVERRIDE])


def _configure_build(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", nargs="?", default="main", help="Source branch or tag (default: main)")
    parser.add_argument("--local", action="store_true", help="Build the existing cached M3Undle checkout without fetching")


@registry.command("build", help="Build and deploy M3Undle with the automated host-network topology", configure=_configure_build)
def handle_build(args: argparse.Namespace, config: object) -> int:
    image = _build_and_deploy(args.target, local=args.local)
    _wait_healthy()
    print(f"M3Undle image {image} built, deployed, and healthy.", flush=True)
    return 0


def _configure_pull(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("tag", help="Published release tag to pull (e.g. v1.0.0-beta.9)")


@registry.command("pull", help="Pull and deploy a published GHCR release tag", configure=_configure_pull)
def handle_pull(args: argparse.Namespace, config: object) -> int:
    image = _pull_and_deploy(args.tag)
    _wait_healthy()
    print(f"M3Undle image {image} pulled, deployed, and healthy.", flush=True)
    return 0


def _configure_up(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", nargs="?", help="Optional source branch or tag to build before bringing up")
    parser.add_argument("--local", action="store_true", help="Build the existing cached checkout without fetching")
    parser.add_argument("--pull", metavar="TAG", help="Pull a published GHCR release tag instead of building from source")


def _validate_up_options(args: argparse.Namespace) -> None:
    if args.target and args.pull:
        raise SystemExit("A source target and --pull are mutually exclusive.")
    if args.pull and args.local:
        raise SystemExit("--pull cannot be combined with --local.")


@registry.command(
    "up",
    help="Deploy M3Undle (building/pulling if a target is given) and leave it running for manual testing",
    configure=_configure_up,
)
def handle_up(args: argparse.Namespace, config: object) -> int:
    _validate_up_options(args)
    if args.pull:
        image = _pull_and_deploy(args.pull)
        _wait_healthy()
    elif args.target or args.local:
        image = _build_and_deploy(args.target, local=args.local)
        _wait_healthy()
    else:
        if lab_common.get_current_image() is None:
            raise SystemExit("No image is deployed. Pass a target, --pull, or run './lab build <branch>' first.")
        _host_compose_up()
        image = lab_common.get_current_image()
    print(f"M3Undle image {image} is up and healthy. Use './lab down' when finished.", flush=True)
    return 0


def _configure_recreate(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fresh", action="store_true", help="Reset only M3Undle's SQLite database")


@registry.command("recreate", help="Restart M3Undle; optionally reset its SQLite database", configure=_configure_recreate)
def handle_recreate(args: argparse.Namespace, config: object) -> int:
    if args.fresh:
        lab_common.run(lab_common.compose_command("down", "--remove-orphans", extra_compose_files=[HOST_OVERRIDE]), check=False)
        registry.get_database_plugin().reset()
    _host_compose_up()
    print("M3Undle recreated and healthy.", flush=True)
    return 0


def _configure_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", nargs="?", help="Optional source branch or tag to build before running")
    parser.add_argument("--local", action="store_true", help="Build the existing cached checkout without fetching")
    parser.add_argument("--pull", metavar="TAG", help="Pull a published GHCR release tag instead of building from source")
    parser.add_argument(
        "--no-deploy",
        action="store_true",
        help="Rerun without rebuilding -- redeploy the currently-tagged image fresh, then run suites",
    )
    parser.add_argument("--fresh", action="store_true", help="Reset only M3Undle's SQLite database before running")
    parser.add_argument("--keep", action="store_true", help="Leave M3Undle running after the run instead of tearing it down")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--only", metavar="SUITE", help="Run one registered suite by name")
    selection.add_argument("--test-group", metavar="GROUP", help="Run a registered suite group (default: all)")
    parser.add_argument("--case", metavar="CASE_ID", help="Narrow to one registered case id within the selected suite(s)")
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip the run-plan confirmation prompt (for CI/automation)"
    )


def _validate_run_options(args: argparse.Namespace) -> None:
    if args.target and args.pull:
        raise SystemExit("A source target and --pull are mutually exclusive.")
    if args.pull and args.local:
        raise SystemExit("--pull cannot be combined with --local.")
    if args.no_deploy and (args.target or args.pull or args.local):
        raise SystemExit("A source target, --pull, or --local cannot be used with --no-deploy.")


def _describe_run_plan(args: argparse.Namespace) -> RunPlan:
    plan = RunPlan(label="M3Undle Lab", host=lab_common.current_hostname())
    plan.add("Runtime dir", str(lab_common.runtime_dir()))
    metadata = lab_common.get_deployment_metadata()
    if metadata["source_type"] and metadata["source_ref"]:
        plan.add("Current recorded source", f"{metadata['source_type']} {metadata['source_ref']}")
    else:
        plan.add("Current recorded source", "none deployed yet")

    if args.pull:
        plan.add("Resolved source", f"GHCR release tag {args.pull}")
        plan.add("Source action", "pull published image, recreate stack")
    elif args.local:
        plan.add("Resolved source", "existing cached checkout (no fetch)")
        plan.add("Source action", "build cached checkout, recreate stack")
    elif args.target:
        plan.add("Resolved source", f"branch or tag {args.target!r}")
        plan.add("Source action", "fetch origin, checkout/reset, build image, recreate stack")
    else:
        plan.add("Resolved source", "currently deployed image (no rebuild)")
        plan.add("Source action", "redeploy current image, recreate stack")

    plan.add("Clean mode", "reset SQLite database" if args.fresh else "none")
    selection = args.only or (f"group {args.test_group}" if args.test_group else "all")
    if args.case:
        selection += f", case {args.case}"
    plan.add("Suites", selection)
    plan.add("Teardown", "leave running (--keep)" if args.keep else "stop after run")
    return plan


@registry.command(
    "run",
    help="Deploy M3Undle fresh, run registered suites, and tear down when done (unless --keep)",
    configure=_configure_run,
)
def handle_run(args: argparse.Namespace, config: object) -> int:
    _validate_run_options(args)
    if not _describe_run_plan(args).confirm(assume_yes=args.yes):
        print("Aborted.", flush=True)
        return 1
    if args.fresh:
        lab_common.run(lab_common.compose_command("down", "--remove-orphans", extra_compose_files=[HOST_OVERRIDE]), check=False)
        registry.get_database_plugin().reset()

    if args.pull:
        _pull_and_deploy(args.pull)
        _wait_healthy()
    elif args.target or args.local:
        _build_and_deploy(args.target, local=args.local)
        _wait_healthy()
    else:
        # --no-deploy, or no target given at all -- both mean "rerun without
        # rebuilding": reuse whatever image is currently tagged as deployed.
        if lab_common.get_current_image() is None:
            raise SystemExit("No image is deployed. Pass a target, --pull, or run './lab build <branch>' first.")
        _host_compose_up()

    selected = select_suites(discover_suites(TESTS_DIR), only=args.only, group=args.test_group, case=args.case)
    summary = run_suites(selected, results_dir=lab_common.runtime_results_dir(), label="M3Undle Lab", base_url=_base_url())

    if args.keep:
        print("M3Undle left running (--keep).", flush=True)
    else:
        lab_common.compose_down()
        print("M3Undle stopped after the run.", flush=True)

    return 1 if summary.failed else 0


def _configure_status(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--logs", type=int, default=0, metavar="LINES", help="Tail M3Undle logs after status")


class M3UndleStatus(BaseStatus):
    def __init__(self, *, logs: int = 0) -> None:
        self._logs = logs

    def extra(self) -> int:
        try:
            with urllib.request.urlopen(f"{_base_url().rstrip('/')}/livez", timeout=5) as response:
                print(f"HTTP health: {response.status}", flush=True)
                healthy = response.status == 200
        except urllib.error.HTTPError as error:
            print(f"HTTP health: {error.code}", flush=True)
            healthy = False
        except OSError as error:
            print(f"HTTP health: unavailable ({error})", flush=True)
            healthy = False
        if self._logs:
            lab_common.compose_logs(self._logs, SERVICE)
        return 0 if healthy else 1


@registry.command("status", help="Show M3Undle Compose state and HTTP health", configure=_configure_status)
def handle_status(args: argparse.Namespace, config: object) -> int:
    return M3UndleStatus(logs=args.logs).run()


def _configure_checklist(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Label for this run (default: the currently deployed source ref, or 'unlabeled')",
    )


@registry.command(
    "checklist",
    help="Generate a manual client-app testing checklist (Jellyfin/NextPVR) for the currently deployed build",
    configure=_configure_checklist,
)
def handle_checklist(args: argparse.Namespace, config: object) -> int:
    """Jellyfin and NextPVR have no automated verify() (see agent/clients/plugin.py's
    manual-only fallback story) -- this is the only coverage they get. Fills in the
    tracked docs/checklist-template.md with the current deployment's own metadata,
    same fields the old frozen lab's `create-checklist` populated by hand, so a
    tester isn't retyping the image/commit/host into a blank template."""
    if not CHECKLIST_TEMPLATE.is_file():
        raise SystemExit(f"Missing {CHECKLIST_TEMPLATE}.")

    metadata = lab_common.get_deployment_metadata()
    image = metadata["image"] or "not deployed"
    digest = lab_common.get_image_repo_digest(metadata["image"]) if metadata["image"] else None
    host = urlsplit(_base_url()).hostname or "127.0.0.1"
    target_label = args.target or metadata["source_ref"] or "unlabeled"

    filled = CHECKLIST_TEMPLATE.read_text(encoding="utf-8").format(
        host=host,
        target_label=target_label,
        source_type=metadata["source_type"] or "unknown",
        source_ref=metadata["source_ref"] or "unknown",
        source_commit=metadata["source_commit"] or "unknown",
        image=image,
        image_digest=digest or "unknown",
        generated_at_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    run_id = lab_common.results_run_id(f"checklist-{target_label}")
    out_path = lab_common.artifacts_checklists_dir() / f"{run_id}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(filled, encoding="utf-8")

    print(f"Checklist written to {out_path}", flush=True)
    print(f"M3Undle: http://{host}:8080  (HDHR manual add: http://{host}:5004)", flush=True)
    return 0
