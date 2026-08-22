# m3undle-lab

Deterministic integration testing for [M3Undle](https://github.com/Sydney-Elvis/M3Undle),
built on [se-lab](https://github.com/Sydney-Elvis/se-lab).

The lab has one shared M3Undle instance. Automated suites use the host-network override;
the future Jellyfin/NextPVR workflow uses the bridge override, whose M3Undle address is
fixed at `172.21.0.2` because NextPVR caches the manually-added tuner IP.

## Quick start

```bash
git submodule update --init
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
generic `down`. `tests/test_auth_gate.py` is the first migrated suite: its registered
`AUTH-01` through `AUTH-07` cases (plus deterministic restoration) can be run with:

```bash
./lab run --fresh --only auth-gate
```

Jellyfin and NextPVR are registered as manual-only client plugins. `lab clients` deployment,
the client compose services, and the remaining frozen suites are intentionally follow-up work.
