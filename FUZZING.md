# Fuzzing

This runbook covers randomized local fuzzing for the routing adapter and its
current VyOS-compatible target profile.

## Goal

The fuzz harness stresses the supported HBR CRD subset with many valid but
varied object combinations. It is not a raw parser fuzzer. Instead, it targets
the adapter properties that matter most here:

- no unexpected crash while loading, translating, reconciling, or statusing
- warnings and unsupported markers instead of silent bad behavior
- clean convergence after a successful apply
- clean second-pass idempotency after that apply

## What It Mutates

The current harness mutates both:

- `NodeNetworkConfig`
- `NodeNetplanConfig`

It randomizes combinations of:

- revisions
- VRF tables and interface attachments
- static routes (IPv4/IPv6, varying prefix lengths)
- policy routes (multi-protocol, ports, nexthop/vrf actions)
- BGP peers with address families (ipv4, ipv6, l2vpn-evpn)
- BGP structured filters (~40% of peers): importFilter/exportFilter with
  prefix matchers (ge/le), community matchers (exact-match), route
  modifications (addCommunities, removeAllCommunities, additive)
- EVPN fields on fabric VRFs (~40%): vni, evpnExportRouteTargets,
  evpnImportRouteTargets, evpnExportFilter, vrfImports
- Layer2 domains (~35%): vni, vlan, mtu, routeTarget, IRB (ipAddresses,
  macAddress, vrf)
- netplan addresses, routes, and nameserver sets
- optional unsupported-but-well-formed fields (unsupported BGP families,
  mirrorAcls)

## Run It

Use the default seed and iteration count:

```bash
python3 scripts/fuzz-hbr-api-local.py run
```

Run a longer campaign:

```bash
python3 scripts/fuzz-hbr-api-local.py run \
  --iterations 400 \
  --seed 20260320
```

## Expected Behavior

A successful run:

- exits `0`
- prints a JSON summary
- writes per-case artifacts under `artifacts/private/hbr-fuzz-local`

For each randomized case it verifies:

- pending reconcile produces exactly two document results
- each document gets a valid desired digest
- apply converges the documents to `in_sync`
- repeat reconcile produces zero pending commands and only `noop` actions

## Artifacts

Artifacts are written under:

```text
artifacts/private/hbr-fuzz-local
```

Each case stores:

- mutated input CRDs
- pending state/status
- applied state/status
- repeat state/status
- per-case summary

The top-level summary is:

```text
artifacts/private/hbr-fuzz-local/summary.json
```

The public-safe summary for publication is:

```text
artifacts/public/hbr-fuzz-local-summary.json
```

## Position In The Test Pyramid

Use fuzzing alongside, not instead of:

- [BOUNDARY.md](BOUNDARY.md)
- [CHAOS.md](CHAOS.md)
- [SMOKE.md](SMOKE.md)

Boundary tests target specific edges.
Chaos tests target failure and recovery.
Fuzzing targets randomized combinatorial drift inside the supported CRD space.

## Live Lab Fuzzing

For real cluster plus VyOS exercise, use:

```bash
scripts/live-fuzz-vyos-vm.sh run
```

This live runner:

- generates randomized HBR CRDs locally
- applies them to the Venus RKE2 cluster
- reconciles them through the Kubernetes-backed controller to the real VyOS VM
- reruns the controller to confirm a clean second-pass `noop`

Useful knobs:

```bash
SEEDS=2 CASES_PER_SEED=10 scripts/live-fuzz-vyos-vm.sh run
```

Artifacts are written under:

```text
artifacts/private/hbr-live-fuzz
```

Treat this as an extended validation layer because it is slower and depends on
the live lab being available.
