# Stable Usage And Test Guide

This document is the operator-facing guide for the current Cloud Connector
routing adapter state. It focuses on how to use the component safely, how to
validate it, and what has already been proven in the lab against the current
VyOS-compatible target profile.

For the consolidated executed-test ledger, corrected failures, and residual
validation gaps, see [VALIDATION.md](VALIDATION.md).

## What This Component Is

The adapter translates a safe subset of `NodeNetworkConfig` and
`NodeNetplanConfig` style objects into routing configuration changes on a
target that is currently compatible with VyOS. It is
designed as an external controller-side component, not as an in-VyOS
replacement for the HBR operator or CRA.

Today it can:

- translate supported HBR-like CRD fields into VyOS `set ...` commands
- reconcile desired versus applied state with a local state file
- export CRD-style status from that state
- patch Kubernetes `status`
- source documents from local files or directly from Kubernetes
- apply changes to a VyOS node over the HTTPS API

## Current Stability Statement

The current adapter is stable for the implemented subset described in
[MAPPING.md](MAPPING.md).

That statement is based on:

- local example and reconcile regression coverage
- local boundary-value coverage
- local chaos/failure-injection coverage
- local randomized fuzzing coverage
- live smoke validation against the VyOS VM
- live repeated lifecycle chaos runs against the VyOS VM in the lab

The strongest live result so far is:

- `2` full lifecycle rounds completed successfully
- each round covered `baseline`, `reboot`, `reset`, and `stop-start`
- each phase ran the API parameter matrix and the deeper smoke harness
- the adapter recovered cleanly after guest reboot, hard reset, and full
  shutdown/start

The validated live target for those campaigns was:

- VyOS `1.5-rolling-202603190955`
- release flavor: `vpp`
- build commit: `96ff51d3d2e559`
- image provenance: built from the author's separate compatible VyOS build workflow

## Supported Operating Model

Use this component when all of these are true:

- Kubernetes CRDs remain the source of truth
- VyOS is the routing/configuration target
- lossy translation is unacceptable
- unsupported fields should become warnings, not silent behavior

Do not treat it yet as:

- a full CRA replacement
- a full HBR implementation
- a full netplan implementation
- a full FRR/BGP parity layer for every HBR object shape

## Prerequisites

For local adapter usage:

- Python `3`
- the adapter installed in a venv or used via `PYTHONPATH`

For live VyOS usage:

- reachable VyOS HTTPS API
- API key enabled on the VyOS node
- management reachability to the VyOS node

For Kubernetes-backed controller usage:

- access to the cluster kubeconfig
- the HBR CRDs installed
- permission to patch the CRD `status` subresource

## Recommended Usage Progression

Use the component in this order:

1. `plan`
2. `reconcile` without `--apply`
3. direct VyOS smoke apply
4. Kubernetes-backed controller once
5. repeated smoke and chaos campaigns

That progression keeps risk low and makes failures easier to localize.

## Quick Start

Create a local venv and install the adapter:

```bash
cd <repo-root>
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Plan from a sample CRD:

```bash
python3 -m hbr_vyos_adapter.cli \
  plan --file examples/node-network-config.json
```

Reconcile without touching VyOS:

```bash
python3 -m hbr_vyos_adapter.cli reconcile \
  --file examples/node-network-config.json \
  --state-file /tmp/hbr-state.json \
  --status-file /tmp/hbr-status.json
```

Apply to a VyOS node:

```bash
export VYOS_URL="https://vyos.example.test"
export VYOS_API_KEY="replace-with-real-api-key"

python3 -m hbr_vyos_adapter.cli reconcile \
  --file examples/node-network-config.json \
  --state-file /tmp/hbr-state.json \
  --apply \
  --vyos-url "$VYOS_URL" \
  --api-key "$VYOS_API_KEY"
```

Run the controller once against Kubernetes:

```bash
python3 -m hbr_vyos_adapter.cli controller \
  --source kubernetes \
  --kubeconfig ~/.kube/config \
  --state-file /tmp/hbr-state.json \
  --status-file /tmp/hbr-status.json \
  --once \
  --write-status \
  --dry-run-status
```

## Live VyOS API Preparation

On the VyOS lab VM, enable the HTTPS API:

```bash
configure
set service https listen-address 192.0.2.230
set service https allow-client address 192.0.2.0/24
set service https api keys id smoke key replace-with-real-api-key
set service https api rest
commit
save
exit
```

Then test reachability:

```bash
curl -k --location --request POST 'https://192.0.2.230/retrieve' \
  --form data='{"op": "showConfig", "path": []}' \
  --form key='replace-with-real-api-key'
```

## Test Layers

### Local Regression

This is the fast confidence layer:

```bash
scripts/check_examples.sh
```

It covers:

- translation snapshots
- reconcile/state/status paths
- Kubernetes document and status writeback behavior
- local chaos scenarios
- local boundary-value scenarios

### Boundary Tests

Boundary tests focus on translator and reconcile edges:

```bash
python3 scripts/boundary-hbr-api-local.py
```

It exercises:

- interface-family edge cases
- route and policy-route boundaries
- BGP edge inputs
- netplan edge inputs
- zero-command reconciliation

See [BOUNDARY.md](BOUNDARY.md) for the scenario list.

### Local Chaos Tests

Chaos tests inject failures without needing the lab VM:

```bash
python3 scripts/chaos-hbr-api-local.py run
```

It covers:

- VyOS apply timeout and recovery
- Kubernetes status patch failure and recovery
- watch churn, deletion, and pruning
- retry behavior for Kubernetes status patch transport and `5xx` failures

See [CHAOS.md](CHAOS.md) for details.

### Local Fuzzing

The fuzz harness stresses randomized but well-formed HBR CRD combinations:

```bash
python3 scripts/fuzz-hbr-api-local.py run
```

It validates:

- no unexpected crash for randomized supported-shape inputs
- reconcile produces stable digests and status
- apply converges
- a second reconcile pass becomes a clean `noop`

See [FUZZING.md](FUZZING.md) for the runbook.

### Live Smoke

The safe live smoke path validates the adapter against the real VM:

```bash
scripts/smoke-vyos-vm.sh plan
scripts/smoke-vyos-vm.sh run
```

Use the smoke-specific example manifests under `examples/`
instead of the broader examples when you want to avoid touching management
interfaces unexpectedly.

See [SMOKE.md](SMOKE.md).

### Deeper Live Smoke

The deeper harness proves more than simple apply:

```bash
scripts/deeper-smoke-vyos-vm.sh run
```

It validates:

- baseline apply
- idempotent re-run
- additive mutation
- CR deletion handling
- explicit cleanup verification

### API Parameter Matrix

This harness mutates one supported parameter family at a time:

```bash
CYCLES=2 VYOS_TIMEOUT=90 \
  scripts/api-parameter-matrix-vyos-vm.sh run
```

It covers:

- revision-only changes
- route table changes
- IPv4 and IPv6 route prefixes
- IPv4 and IPv6 next-hop changes
- BGP peer shapes
- policy-route variations
- nameserver variations

This is the best direct test for “does a small CRD change produce a small and
correct VyOS change”.

### Lifecycle Chaos

This is the strongest live test currently available:

```bash
scripts/lifecycle-api-matrix-vyos-vm.sh run
```

It runs repeated rounds of:

- baseline
- guest reboot
- hard guest reset
- full guest stop/start

After each lifecycle event, it reruns:

- the API parameter matrix
- the deeper smoke harness

### Live Fuzzing

This is the next-level extended gate when you want randomized live CRD
coverage on top of the deterministic matrix:

```bash
scripts/live-fuzz-vyos-vm.sh run
```

It generates randomized but well-formed `NodeNetworkConfig` and
`NodeNetplanConfig` pairs, applies them through the Kubernetes-backed
controller to the real VyOS VM, then runs a second pass to confirm clean
convergence.

Use it when you want:

- broader combinatorial coverage than the fixed matrix provides
- real cluster plus VyOS API exercise
- a stronger burn-in campaign before wider use

## What A Good Run Looks Like

A good run has these characteristics:

- the VyOS API becomes temporarily unavailable during reboot/reset/stop-start
- the harness waits for recovery
- the next matrix cases apply successfully
- deeper smoke still passes idempotency and mutation checks
- CR deletion still converges
- the harness exits `0`

Temporary `curl` failures during guest transitions are expected. They are part
of the lifecycle chaos scenario, not a test failure by themselves.

Temporary `urllib3` insecure HTTPS warnings are also expected in the current
lab because the VyOS API is accessed with unverified TLS.

## Lab-Validated Result

The current live validation already proved:

- `2` lifecycle rounds completed successfully
- `baseline`, `reboot`, `reset`, and `stop-start` all passed
- the full API matrix passed after each lifecycle event
- the deeper smoke harness passed after each lifecycle event

Artifacts for the live lifecycle campaign are stored under:

```text
artifacts/private/hbr-lifecycle
```

Local chaos artifacts are stored under:

```text
artifacts/private/hbr-chaos-local
```

Local boundary artifacts are stored under:

```text
artifacts/private/hbr-boundary-local
```

Public-safe summaries are stored under:

```text
artifacts/public
```

## Recommended Release Gate

Before calling the adapter stable for a given environment, run at least:

1. `scripts/check_examples.sh`
2. `python3 scripts/chaos-hbr-api-local.py run`
3. `python3 scripts/boundary-hbr-api-local.py`
4. `python3 scripts/fuzz-hbr-api-local.py run`
5. `scripts/smoke-vyos-vm.sh run`
6. `scripts/deeper-smoke-vyos-vm.sh run`
7. `scripts/lifecycle-api-matrix-vyos-vm.sh run`

For an extended gate, add:

8. `scripts/live-fuzz-vyos-vm.sh run`

If you need a shorter but still meaningful gate, use:

1. local regression
2. local chaos
3. one live smoke run
4. one lifecycle run

## Known Limits

The component is still intentionally conservative.

Known limits:

- unsupported HBR fields are warnings, not translated behavior
- full CRA semantics are not implemented
- full netplan parity is not implemented
- full L2/VNI semantics are not implemented
- status writeback depends on Kubernetes API permissions and CRD shape
- the lab currently uses insecure HTTPS to the VyOS API

## Where To Start Reading

If you are new to the adapter, read in this order:

1. [README.md](README.md)
2. [STABLE.md](STABLE.md)
3. [MAPPING.md](MAPPING.md)
4. [SMOKE.md](SMOKE.md)
5. [CHAOS.md](CHAOS.md)

If you are changing the translator or controller, also read:

1. [ANALYSIS.md](ANALYSIS.md)
2. [PROJECT.md](PROJECT.md)
