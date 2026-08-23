# m3undle-lab

Deterministic integration testing for [M3Undle](https://github.com/Sydney-Elvis/M3Undle),
built on [se-lab](https://github.com/Sydney-Elvis/se-lab).

The lab has one shared M3Undle instance. Automated suites use the host-network override;
the future Jellyfin/NextPVR workflow uses the bridge override, whose M3Undle address is
fixed at `172.21.0.2` because NextPVR caches the manually-added tuner IP.

## Quick start

```bash
git submodule update --init

# First-time environment setup (venv + dependencies) is se-lab's job, not
# duplicated here. See se-lab/README.md's Quick Start for the manual venv
# steps, or run `se-lab/scripts/setup_vm.sh --product-name m3undle
# --env-prefix M3UNDLE` on a fresh host (also runs a preflight check).

cp lab.env.example lab.env
# Set M3UNDLE_HDHR_ADVERTISED_BASE_URL to this host's LAN address.
./lab build main
./lab run --fresh
./lab status
./lab down
```

`M3UNDLE_REPO_URL` is optional: the M3Undle plugin defaults it to the public upstream.
`M3UNDLE_LAB_ROLE` is deliberately unsupported; se-lab and this lab have one stack, not
srv1/srv2 dispatch.

## Current migration slice

The initial lifecycle commands are `build`, `run`, `recreate`, `status`, and se-lab's
generic `down`. `lab run` now executes every registered suite by default; use
`--test-group` or `--only` to narrow that selection, and `--deploy-only` when an
update should stop after health verification. `tests/test_auth_gate.py` is the first
migrated suite: its registered `AUTH-01` through `AUTH-07` cases (plus deterministic
restoration) can be run with:

```bash
./lab run --fresh --only auth-gate
# Future ports may be selected as a group, for example:
./lab run --test-group core
# Or deploy a known image without executing suites:
./lab run --deploy-only
```

Jellyfin and NextPVR are registered as manual-only client plugins. `lab clients` deployment,
the client compose services, and the remaining frozen suites are intentionally follow-up work.

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
