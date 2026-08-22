"""M3Undle lifecycle commands built on se-lab's generic primitives."""

from __future__ import annotations

import argparse
import urllib.error
import urllib.request
from pathlib import Path

from agent import common as lab_common, registry
from agent.container import wait_up
from agent.results import RunContext
from agent.suites import discover_suites, run_suite

from .analysis import M3UndleAnalysis
from .database import M3UndleDatabase
from .settings import M3UndleSettings
from . import clients as _clients  # noqa: F401 - class decorators register product clients


DEFAULT_REPO_URL = "https://github.com/Sydney-Elvis/M3Undle.git"
SERVICE = "m3undle"
CONTAINER_NAME = "m3undle-lab"
HOST_OVERRIDE = Path(__file__).resolve().parents[1] / "docker-config" / "m3undle-host-network.override.yaml"
TESTS_DIR = Path(__file__).resolve().parents[1] / "tests"

registry.set_analysis_plugin(M3UndleAnalysis())
registry.set_database_plugin(M3UndleDatabase())
registry.set_settings_plugin(M3UndleSettings())


def _layout() -> None:
    """Create M3Undle's product directories and project lab.env into runtime .env."""
    runtime = lab_common.runtime_dir()
    (runtime / "m3u_data").mkdir(parents=True, exist_ok=True)
    values = lab_common.load_lab_env()
    product_values = {name: value for name, value in values.items() if name.startswith("M3UNDLE_")}
    if product_values:
        # This hook runs from common.ensure_layout(), so calling
        # set_runtime_env_values() here would recurse back into this hook.
        lab_common.write_env_file_values(lab_common.runtime_env_file(), product_values)


registry.set_layout_hook(_layout)


def _base_url() -> str:
    return lab_common.resolve_setting("M3UNDLE_BASE_URL", default="http://127.0.0.1:8080") or "http://127.0.0.1:8080"


def _ensure_repo_checkout(repo_url: str) -> Path:
    """Use the generic path/naming helpers without the current se-lab clone bug.

    se-lab's current ``ensure_repo_checkout()`` assigns ``repo_dir = repo_dir()``
    and therefore raises UnboundLocalError before it can clone.  Keep the
    workaround local to this product plugin until the framework lands its own
    fix, rather than modifying the frozen predecessor or carrying a dirty
    submodule implementation.
    """
    checkout = lab_common.repo_dir()
    if checkout.exists():
        if not lab_common.is_git_checkout(checkout):
            raise SystemExit(f"{checkout} exists but is not a git checkout. Move it aside before building.")
        return checkout
    lab_common.run(["git", "clone", repo_url, str(checkout)])
    return checkout


def _resolve_target(target: str, repo_dir: Path) -> tuple[str, str]:
    """Classify target with the frozen harness's tag-before-branch semantics."""
    lab_common.run(["git", "fetch", "--prune", "--tags", "origin"], cwd=repo_dir)
    if lab_common.git_ref_exists(f"refs/tags/{target}"):
        return "tag", target
    if lab_common.git_ref_exists(f"refs/remotes/origin/{target}"):
        return "branch", target
    raise SystemExit(f"{target!r} is neither a tag nor a branch on origin after fetching.")


def _prepare_branch(branch: str, repo_dir: Path) -> None:
    """Prepare the disposable cached checkout without se-lab's shadowed helper."""
    lab_common.run(["git", "fetch", "--prune", "origin"], cwd=repo_dir)
    if not lab_common.git_ref_exists(f"refs/remotes/origin/{branch}"):
        raise SystemExit(f"origin/{branch} does not exist after fetching origin.")
    lab_common.run(["git", "reset", "--hard"], cwd=repo_dir)
    lab_common.run(["git", "clean", "-ffdx"], cwd=repo_dir)
    lab_common.run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir)
    lab_common.run(["git", "reset", "--hard", f"origin/{branch}"], cwd=repo_dir)


def _prepare_tag(tag: str, repo_dir: Path) -> None:
    lab_common.run(["git", "fetch", "--prune", "--tags", "origin"], cwd=repo_dir)
    if not lab_common.git_ref_exists(f"refs/tags/{tag}"):
        raise SystemExit(f"Tag {tag!r} does not exist after fetching origin.")
    lab_common.run(["git", "reset", "--hard"], cwd=repo_dir)
    lab_common.run(["git", "clean", "-ffdx"], cwd=repo_dir)
    lab_common.run(["git", "checkout", "--detach", f"refs/tags/{tag}"], cwd=repo_dir)


def _wait_healthy() -> None:
    if not wait_up(_base_url(), CONTAINER_NAME, health_paths=("/livez", "/health")):
        raise SystemExit("M3Undle did not become healthy. Run './lab status --logs 100' for diagnostics.")


def _host_compose_up() -> None:
    lab_common.compose_up(extra_compose_files=[HOST_OVERRIDE])
    _wait_healthy()


def _configure_build(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", nargs="?", default="main", help="Source branch or tag (default: main)")
    parser.add_argument("--local", action="store_true", help="Build the existing cached M3Undle checkout without fetching")


def _configure_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", nargs="?", help="Optional source branch or tag to build before running")
    parser.add_argument("--local", action="store_true", help="Build the existing cached checkout without fetching")
    parser.add_argument("--fresh", action="store_true", help="Reset only M3Undle's SQLite database before running")
    parser.add_argument("--only", help="Run one registered suite by name after readiness succeeds")
    parser.add_argument("--no-deploy", action="store_true", help="Use the already-running M3Undle stack")


def _configure_recreate(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fresh", action="store_true", help="Reset only M3Undle's SQLite database")


def _configure_status(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--logs", type=int, default=0, metavar="LINES", help="Tail M3Undle logs after status")


def _build(target: str, *, local: bool) -> str:
    repo_url = lab_common.resolve_setting("M3UNDLE_REPO_URL", default=DEFAULT_REPO_URL) or DEFAULT_REPO_URL
    repo_dir = _ensure_repo_checkout(repo_url)
    if local:
        branch = lab_common.repo_current_branch() or "local"
        image = lab_common.local_branch_image(branch)
        source_type, source_ref = "local", branch
    else:
        kind, ref = _resolve_target(target, repo_dir)
        if kind == "tag":
            _prepare_tag(ref, repo_dir)
            image = lab_common.local_tag_image(ref)
            source_type = "source-tag"
        else:
            _prepare_branch(ref, repo_dir)
            image = lab_common.local_branch_image(ref)
            source_type = "branch"
        source_ref = ref
    commit = lab_common.repo_head_commit()
    lab_common.docker_build(image, repo_dir, source_revision=commit)
    lab_common.sync_runtime_compose()
    lab_common.set_deployment_metadata(source_type, source_ref, image=image, source_commit=commit)
    return image


@registry.command("build", help="Build and deploy M3Undle with the automated host-network topology", configure=_configure_build)
def handle_build(args: argparse.Namespace, config: object) -> int:
    image = _build(args.target, local=args.local)
    _host_compose_up()
    print(f"M3Undle image {image} built, deployed, and healthy.", flush=True)
    return 0


@registry.command("recreate", help="Restart M3Undle; optionally reset its SQLite database", configure=_configure_recreate)
def handle_recreate(args: argparse.Namespace, config: object) -> int:
    if args.fresh:
        lab_common.run(lab_common.compose_command("down", "--remove-orphans", extra_compose_files=[HOST_OVERRIDE]), check=False)
        registry.get_database_plugin().reset()
    _host_compose_up()
    print("M3Undle recreated and healthy.", flush=True)
    return 0


@registry.command("run", help="Deploy or reuse M3Undle, verify health, and optionally run registered suites", configure=_configure_run)
def handle_run(args: argparse.Namespace, config: object) -> int:
    if not args.no_deploy and args.target:
        _build(args.target, local=args.local)
        _host_compose_up()
    elif args.fresh:
        handle_recreate(argparse.Namespace(fresh=True), config)
    elif not args.no_deploy:
        if lab_common.get_current_image() is None:
            raise SystemExit("No image is deployed. Run './lab build <branch>' first, or pass a target to './lab run'.")
        _host_compose_up()
    else:
        _wait_healthy()

    if not args.only:
        print("M3Undle is healthy.", flush=True)
        return 0

    selected = [suite for suite in discover_suites(TESTS_DIR) if suite.name == args.only]
    if not selected:
        raise SystemExit(f"Unknown suite {args.only!r}. Available: {', '.join(s.name for s in discover_suites(TESTS_DIR))}")
    suite = selected[0]
    context = RunContext(suite.name)
    result = run_suite(suite, context, base_url=_base_url())
    context.print_summary()
    context.write_json(lab_common.runtime_results_dir() / f"results-{suite.name}.json")
    return 1 if context.failed_count or result.drifted else 0


@registry.command("status", help="Show M3Undle Compose state and HTTP health", configure=_configure_status)
def handle_status(args: argparse.Namespace, config: object) -> int:
    metadata = lab_common.get_deployment_metadata()
    print(f"Runtime: {lab_common.runtime_summary()}", flush=True)
    print(f"Current configured image: {lab_common.get_current_image() or 'not set'}", flush=True)
    if metadata["source_type"] and metadata["source_ref"]:
        print(f"Deployment source: {metadata['source_type']} {metadata['source_ref']}", flush=True)
    lab_common.compose_ps()
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
    if args.logs:
        lab_common.compose_logs(args.logs, SERVICE)
    return 0 if healthy else 1
