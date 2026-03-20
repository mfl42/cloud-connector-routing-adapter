# Live Smoke Run

This runbook is the safest way to validate the adapter against a real
Kubernetes API and a real VyOS node.

## Safety First

The original example files in `examples/node-network-config.json` and
`examples/node-netplan-config.json` are useful for translator coverage, but
they are not safe for a live smoke run on the current lab VM because they touch
`eth1`, which is the management interface.

Use these smoke manifests instead:

- `examples/node-network-config-smoke.json`
- `examples/node-netplan-config-smoke.json`
- `examples/node-network-config-smoke.yaml`
- `examples/node-netplan-config-smoke.yaml`

They avoid interface reassignment and limit the apply path to:

- one non-default VRF table
- non-default-table static routes
- a single existing name-server entry

## Prerequisites

- a reachable VyOS API endpoint
- a Kubernetes cluster
- `python3.11+`
- `requests` and `PyYAML`

If the target cluster does not already provide the HBR CRDs, install the lab
test CRDs first:

```bash
kubectl apply -f k8s/crds/node-network-config-crd.yaml
kubectl apply -f k8s/crds/node-netplan-config-crd.yaml
```

These CRDs are intentionally permissive and are meant for adapter smoke
testing, not production schema enforcement.

Create a virtual environment and install the local package:

```bash
cd <repo-root>
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

There is also a helper script for the direct VyOS smoke path:

```bash
scripts/smoke-vyos-vm.sh plan
scripts/smoke-vyos-vm.sh api-test
scripts/smoke-vyos-vm.sh apply
```

For repeated end-to-end validation against Venus plus the VyOS VM, use the
deeper harness:

```bash
scripts/deeper-smoke-vyos-vm.sh run
```

It performs multiple baseline/mutate/delete cycles in a dedicated Kubernetes
namespace and saves per-iteration controller, cluster, and VyOS artifacts.

For stronger per-parameter API coverage, use the matrix harness:

```bash
scripts/api-parameter-matrix-vyos-vm.sh run
```

It changes one supported API input at a time across repeated cycles, re-runs
the controller to prove idempotency, checks CRD status writeback, reads the
effective VyOS config back via the REST API, and then deletes and cleans up the
case before moving to the next parameter. If Venus or the LAN is unavailable,
the script now fails fast on an SSH preflight before attempting `rsync`.

You can increase the controller-side VyOS timeout for stress runs with:

```bash
VYOS_TIMEOUT=90 scripts/api-parameter-matrix-vyos-vm.sh run
```

For VM lifecycle resilience, use the lifecycle wrapper:

```bash
scripts/lifecycle-api-matrix-vyos-vm.sh run
```

It runs the same API parameter matrix four times:

- against the current running VM
- after `virsh reboot`
- after `virsh reset`
- after `virsh shutdown` followed by `virsh start`

By default it performs multiple lifecycle rounds, captures `virsh dominfo` plus
VyOS config snapshots before and after each phase, and can chain the deeper
smoke harness after every phase so the adapter is revalidated beyond simple VM
availability.

Useful knobs:

```bash
ROUNDS=3 MATRIX_CYCLES=2 DEEPER_ITERATIONS=2 \
  scripts/lifecycle-api-matrix-vyos-vm.sh run
```

Disable the deeper harness if you only want the API matrix:

```bash
RUN_DEEPER_HARNESS=0 scripts/lifecycle-api-matrix-vyos-vm.sh run
```

## Enable VyOS HTTP API

On the VyOS VM, enable the REST API and bind it to the management address:

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

Quick API test from your workstation:

```bash
curl -k --location --request POST 'https://192.0.2.230/retrieve' \
  --form data='{"op": "showConfig", "path": []}' \
  --form key='replace-with-real-api-key'
```

## Phase 1: Translator Only

Validate the smoke manifests locally without touching VyOS or Kubernetes:

```bash
python3 -m hbr_vyos_adapter.cli plan \
  --file examples/node-network-config-smoke.json
```

```bash
python3 -m hbr_vyos_adapter.cli plan \
  --file examples/node-netplan-config-smoke.json
```

## Phase 2: Apply To VyOS Without Kubernetes

Apply the safe manifests directly to VyOS first:

```bash
export VYOS_URL=https://192.0.2.230
export VYOS_API_KEY=replace-with-real-api-key
```

```bash
python3 -m hbr_vyos_adapter.cli reconcile \
  --file examples/node-network-config-smoke.json \
  --state-file /tmp/hbr-smoke-state.json \
  --status-file /tmp/hbr-smoke-status.json \
  --apply \
  --vyos-url "$VYOS_URL" \
  --api-key "$VYOS_API_KEY"
```

```bash
python3 -m hbr_vyos_adapter.cli reconcile \
  --file examples/node-netplan-config-smoke.json \
  --state-file /tmp/hbr-smoke-state.json \
  --status-file /tmp/hbr-smoke-status.json \
  --apply \
  --vyos-url "$VYOS_URL" \
  --api-key "$VYOS_API_KEY"
```

Verify on VyOS:

```bash
show configuration commands | match "vrf name 'smoke-a'"
show configuration commands | match "table '1000'"
show configuration commands | match "name-server '192.0.2.53'"
```

## Phase 3: Kubernetes Read + Dry-Run Status

Load the smoke manifests into the cluster:

```bash
kubectl apply -f examples/node-network-config-smoke.json
kubectl apply -f examples/node-netplan-config-smoke.json
```

Run a one-shot controller iteration that reads from Kubernetes, writes local
state/status files, and only dry-runs Kubernetes status patching:

```bash
python3 -m hbr_vyos_adapter.cli controller \
  --source kubernetes \
  --kubeconfig ~/.kube/config \
  --source-namespace default \
  --state-file /tmp/hbr-k8s-state.json \
  --status-file /tmp/hbr-k8s-status.json \
  --once \
  --write-status \
  --dry-run-status \
  --json
```

If your CRDs are cluster-scoped, add:

```bash
--cluster-scoped-source --cluster-scoped-status
```

## Phase 4: Full End-To-End Controller

Once phases 1 through 3 are clean, run a live apply from Kubernetes to VyOS:

```bash
python3 -m hbr_vyos_adapter.cli controller \
  --source kubernetes \
  --kubeconfig ~/.kube/config \
  --source-namespace default \
  --state-file /tmp/hbr-k8s-state.json \
  --status-file /tmp/hbr-k8s-status.json \
  --once \
  --apply \
  --vyos-url "$VYOS_URL" \
  --api-key "$VYOS_API_KEY" \
  --write-status \
  --dry-run-status \
  --deleted-retention-seconds 300 \
  --json
```

That applies to VyOS for real, but still only dry-runs the Kubernetes status
patch so you can inspect the payload first.

When you are comfortable with the output, remove `--dry-run-status`.

## Recommended Order

1. translator-only
2. direct VyOS apply with smoke manifests
3. Kubernetes source with dry-run status
4. full controller with apply
5. real status patching

## Cleanup

Delete the smoke resources:

```bash
kubectl delete -f examples/node-network-config-smoke.json
kubectl delete -f examples/node-netplan-config-smoke.json
```

Delete the lab CRDs too if you installed them only for this smoke test:

```bash
kubectl delete -f k8s/crds/node-network-config-crd.yaml
kubectl delete -f k8s/crds/node-netplan-config-crd.yaml
```

Remove the smoke config from VyOS manually if desired:

```bash
configure
delete vrf name smoke-a
commit
save
exit
```
