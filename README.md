# m3undle-lab

Deterministic integration testing for [M3Undle](https://github.com/Sydney-Elvis/M3Undle),
built on [se-lab](https://github.com/Sydney-Elvis/se-lab).

The lab has one shared M3Undle instance. Automated suites use the host-network override;
`lab clients up` uses the bridge override, whose M3Undle address is fixed at `172.21.0.2`
because NextPVR caches the manually-added tuner IP.

## Quick start

```bash
git submodule update --init

# First-time environment setup (venv + dependencies) is se-lab's job, not
# duplicated here. See se-lab/README.md's Quick Start for the manual venv
# steps, or run `se-lab/scripts/setup_vm.sh --product-name m3undle
# --env-prefix M3UNDLE` on a fresh host (also runs a preflight check).

cp lab.env.example lab.env
# Set M3UNDLE_HDHR_ADVERTISED_BASE_URL to this host's LAN address.
./lab up main
./lab status
./lab down
```

`M3UNDLE_REPO_URL` is optional: the M3Undle plugin defaults it to the public upstream.
`M3UNDLE_LAB_ROLE` is deliberately unsupported; se-lab and this lab have one stack, not
srv1/srv2 dispatch.

## Current migration slice

The lifecycle commands are `build`, `pull`, `up`, `run`, `recreate`, `status`, and se-lab's
generic `down` — these bring up M3Undle itself (the product under test). `clients` is a
separate concept: the third-party downstream apps (Jellyfin, NextPVR) that consume
M3Undle's output, not M3Undle itself — see the "Provider simulator" section below and
`./lab clients --help`.

`build`/`up`/`run` share one build/deploy path: `<ref>` is resolved as a tag first, then a
branch, so `./lab up main`, `./lab up mybranch`, and `./lab up v1.0.0-beta.9` all work the
same way. `pull <tag>`/`up --pull <tag>`/`run --pull <tag>` instead pull the actual
published GHCR image for a release tag — a real, distinct check from building at that tag's
commit, since the two can diverge (a platform-specific build issue, a broken publish step).

`up` and `run` are two different jobs, not two names for the same thing:

- `up [ref] [--pull TAG] [--local]` builds/pulls if given a target (or reuses the currently
  deployed image if not), deploys, health-checks, and **leaves M3Undle running** — for manual,
  hands-on testing. Pair it with `./lab down` when you're done.
- `run [ref] [--pull TAG] [--local] [--no-deploy] [--only X] [--test-group G] [--case ID] [--keep]` is
  **only** about automated testing: deploys fresh, runs the selected suites, and always tears
  down afterward — pass or fail — unless `--keep`. `--no-deploy` reruns against the currently
  deployed image instead of rebuilding, but still redeploys fresh, still runs suites, and
  still tears down.

A typical release flow, each step left running only long enough to check it, then torn down
by `run` itself:

```bash
./lab run mybranch              # branch under development
# ... merge to main ...
./lab run main                  # re-verify against main
# ... tag and let CI publish ...
./lab run --pull v1.0.0-beta.9  # final check against the actual published artifact
```

`lab run` executes every registered suite by default; use `--test-group` or `--only` to
narrow that selection, and `--case` to narrow further to one registered case id within
whatever suite(s) are selected (e.g. `--only auth-gate --case AUTH-03`). `tests/test_auth_gate.py`
is the first migrated suite: its registered `AUTH-01` through `AUTH-07` cases (plus
deterministic restoration) can be run with:

```bash
./lab run --fresh --only auth-gate
# Future ports may be selected as a group, for example:
./lab run --test-group core
# Or leave it running afterward to inspect a failure:
./lab run --only auth-gate --keep
# Or bring an instance up for manual testing instead of running suites at all:
./lab up main
./lab down
```

Jellyfin and NextPVR are registered as manual-only client plugins (no automated `verify()` —
se-lab falls back to checklist generation for them). Bringing them up alongside M3Undle:

```bash
./lab clients up                       # every registered client (jellyfin + nextpvr)
./lab clients up --profile jellyfin    # just one
./lab clients status                   # versions + rollback history
./lab clients reset --profile nextpvr  # wipe state, recreate clean
./lab clients down                     # stop/remove clients, M3Undle keeps running
```

`lab clients up/down/reset` switch the whole stack (M3Undle + selected clients) to the
bridge-network topology above, recreating the M3Undle container in the process — don't run
them while an automated suite run (host-network topology) is relying on it staying up. No
scenario matrix yet (one fixed config per client); the remaining frozen suites are still
follow-up work.

Since Jellyfin/NextPVR have no automated `verify()`, `./lab checklist [target]` generates a
working copy of `docs/checklist-template.md` — filled in with the currently deployed
image/commit/digest and the lab host — under `$M3UNDLE_RUNTIME_DIR/artifacts/checklists/`,
for a human to work through against `./lab clients up`.

## Provider simulator + test driver

`m3undle_lab/simulator.py` (`SimulatorInstance`) and `m3undle_lab/api.py` (`M3UndleClient`)
are ported from the frozen lab's harness — most suites need both. The simulator engine
itself (`provider_sim.py`) is a separate public product, not lab code:
[Sydney-Elvis/M3Undle-provider-simulator](https://github.com/Sydney-Elvis/M3Undle-provider-simulator).
Clone it and point `M3UNDLE_SIMULATOR_ENGINE_DIR` at the checkout in `lab.env`. Its local
backend also needs `jsonschema` in this lab's own `.venv` — installed automatically from this
repo's own `requirements.txt` if you use `se-lab/scripts/setup_vm.sh`; otherwise `pip install
-r requirements.txt` after se-lab's own setup.

```python
from m3undle_lab.simulator import SimulatorInstance
from m3undle_lab.api import M3UndleClient

sim = SimulatorInstance(fixture="fixtures/providers/provider-a.json", port=19001, bind="0.0.0.0",
                         public_host="http://host.docker.internal:19001")  # see note below
sim.start()
sim.wait_healthy()

client = M3UndleClient("http://127.0.0.1:8080")
client.setup(playlist_url=sim.playlist_url)   # full provider/profile/snapshot bring-up
client.get_stream_urls()                       # ready for streaming assertions
sim.stop()
```

**`public_host` differs by platform, and this matters more than it looks.** The simulator
needs to bind an address the *M3Undle container* can reach, not just the host. On real Linux
(srv1), `agent.container.get_docker_gateway("m3undle-lab_media")` — the bridge network's own
gateway IP — works, matching `get_docker_gateway()`'s documented behavior. On macOS with
Docker Desktop, that gateway IP is internal to the Desktop VM and does **not** route back to a
process bound on the actual host — use `http://host.docker.internal:{port}` instead (Docker
Desktop's own documented mechanism for exactly this). Verified for real on this Mac: the
gateway-IP approach failed with `Connection refused`; `host.docker.internal` worked end to
end (`M3UndleClient.setup()` completed, 3 channels selected, a real stream byte-fetched
through M3Undle from the simulator).

## Seed connection settings

Once an image with settings archives is deployed, a clean lab instance can be seeded from a
synthetic fixture without restoring mappings, users, history, or caches:

```bash
./lab run --fresh
./lab settings import fixtures/settings/lab-baseline.m3undle-backup
```

Create or refresh the fixture with `./lab settings export --out fixtures/settings/lab-baseline.m3undle-backup`.
The fixture must contain lab-only endpoints and synthetic credentials. Set one stable, lab-only
`M3UNDLE_ENCRYPTION_KEY` in `lab.env` before creating it, and keep that key unchanged while the
fixture is in use.

## Before adding new lifecycle code here

This lab is meant to stay a thin consumer of se-lab — `commands.py` should be orchestration
(argument parsing, calling `agent.common`/`agent.suites`/registered plugins) plus real M3Undle
facts (ports, health paths, image names), not a second copy of subprocess/compose/env-loading
mechanism. Before adding a new one of those here, read se-lab's `docs/design.md` — specifically
its "Guardrail: Where New Lifecycle Code Belongs" section — and check whether `agent.common`,
`agent.suites`, `agent.status`, or an existing plugin ABC already covers it. This lab is currently
the reference example of that pattern working (a sister lab, family-librarian-lab, drifted from it
and an audit had to find and fix a real bug as a result) — keep it that way, and bump this lab's
se-lab submodule pin regularly so mechanism extracted from elsewhere actually reaches this repo.
