# Cloud Connector Routing Adapter

An external Cloud Connector / HBR-style routing adapter currently
compatible with VyOS.

This project provides an independent controller-side adapter that maps
`NodeNetworkConfig` and `NodeNetplanConfig` style resources onto a routing
target through a transparent, testable translation layer. It is designed to
work with the current VyOS-compatible profile without presenting itself as an
official VyOS project.

The current goal is not to replace the HBR operator or CRA directly inside
the target NOS. Instead, this scaffold provides an independent adapter that can:

- load `NodeNetworkConfig` and `NodeNetplanConfig` style documents
- translate a supported subset into VyOS `set ...` commands
- track desired versus last-applied revision state in a local reconcile file
- render a CRD-style status report from that local state
- generate and optionally push CRD `status` patches to Kubernetes
- consume CRD documents either from files or directly from the Kubernetes API
- optionally send those commands to a VyOS instance over its HTTP API

## Status

All planned ROADMAP items (1-9) are implemented. See [FEATURES.md](FEATURES.md)
for a full description in plain language.

- `NodeNetworkConfig`
  - VRF table creation, interface-to-VRF attachment (including VLAN sub-interfaces)
  - static route generation (IPv4/IPv6, next-hop address or interface)
  - policy-route generation (multi-protocol, ports, nexthop/vrf/table actions)
  - full BGP neighbor configuration (ASN, timers, password, bfd, graceful-restart)
  - BGP import/export filter compilation (route-map, prefix-list, community-list)
  - Layer2/VXLAN domains (vni, bridge, IRB with IP/MAC/VRF)
  - EVPN on fabric VRFs (l2vpn-evpn, route targets, advertise-all-vni, VRF imports)
- `NodeNetplanConfig`
  - interface addresses, DHCP v4/v6, MTU
  - static routes with metric, default routes
  - name-server configuration
  - dual-format: legacy `spec.interfaces` and netplan native `spec.desiredState`
- reconcile/status layer
  - digest-based change detection, diff-based partial delete, full teardown
  - rollback on apply failure (discard pending VyOS state)
  - CRA status conditions: Available, Reconciling, Degraded, InSync, etc.
- controller infrastructure
  - background informer watch thread with event queue and exponential backoff
  - Lease-based leader election for multi-instance coordination
  - cluster-scoped and namespace-scoped CRD watch
  - Kubernetes status subresource patching with retry/backoff

Remaining unsupported:
- simple string route-map references (`routeMap`, `prefixList`)
- `mirrorAcls` GRE traffic mirroring
- VTEP source-address (not in CRD; VyOS uses loopback)
- per-interface readiness in status conditions

See [ROADMAP.md](ROADMAP.md) for the full list of limitations and next steps.

## Documentation

- [FEATURES.md](FEATURES.md)
  - description complete des fonctionnalites en langage simple
- [STABLE.md](STABLE.md)
  - consolidated usage, test, stability, and live validation guide for the current adapter
- [VALIDATION.md](VALIDATION.md)
  - consolidated validation ledger covering campaigns executed, fixes made, evidence, and residual gaps
- [FUZZING.md](FUZZING.md)
  - randomized local fuzzing guide for supported HBR CRD combinations
- [PROJECT.md](PROJECT.md)
  - project scope, goals, architecture, and implementation status
- [ANALYSIS.md](ANALYSIS.md)
  - problem analysis, option tradeoffs, why an external adapter is the right first design
- [MAPPING.md](MAPPING.md)
  - field-by-field summary of the currently implemented CRD-to-target translation for the current VyOS-compatible profile
- [SMOKE.md](SMOKE.md)
  - safe live smoke-run procedure for Kubernetes plus a VyOS-compatible target
- [BOUNDARY.md](BOUNDARY.md)
  - boundary-value runbook for translator and reconcile edge cases
- [CHAOS.md](CHAOS.md)
  - failure-injection runbook for local adapter chaos and live VM lifecycle chaos

## Licensing

This repository is licensed under Apache License 2.0.
See [LICENSE](LICENSE).

Copyright 2026 Michel Besnard.
See [NOTICE](NOTICE).

This repository is intended to be published as a standalone project.

## Compatibility

This project is an independent adapter that is currently validated as
compatible with VyOS. It is not an official VyOS project.

## Validated Target Provenance

The current live validation target was:

- VyOS `1.5-rolling-202603190955`
- release flavor: `vpp`
- build commit: `96ff51d3d2e559`
- built on `2026-03-19`
- exercised through the VyOS HTTPS API
- produced from the author's separate compatible VyOS build workflow

## Layout

- `hbr_vyos_adapter/models.py`
  - tolerant document models for HBR-like resources
- `hbr_vyos_adapter/translator.py`
  - converts supported resource subsets into VyOS commands
- `hbr_vyos_adapter/reconcile.py`
  - compares desired state against local applied state and optionally applies changes
- `hbr_vyos_adapter/state.py`
  - persistent local state file model for revision/digest tracking
- `hbr_vyos_adapter/status.py`
  - CRD-style status report rendering from local reconcile state
- `hbr_vyos_adapter/k8s_status.py`
  - Kubernetes status-subresource patch generation, writeback, and retry/backoff
- `hbr_vyos_adapter/k8s_documents.py`
  - Kubernetes CRD list/watch document source with stale-watch recovery
- `hbr_vyos_adapter/controller.py`
  - control loop that orchestrates reconcile, status export, optional writeback, and file or Kubernetes sourcing
- `hbr_vyos_adapter/vyos_api.py`
  - minimal VyOS HTTP API client
- `hbr_vyos_adapter/cli.py`
  - local plan, reconcile, status, write-status, controller, and apply entrypoints
- `scripts/smoke-vyos-vm.sh`
  - helper for the safe direct smoke path against the lab VM
- `scripts/deeper-smoke-vyos-vm.sh`
  - repeatable multi-iteration live harness covering apply, idempotency, additive mutation, tombstones, and VyOS/Kubernetes verification
- `scripts/api-parameter-matrix-vyos-vm.sh`
  - repeated live matrix harness that mutates individual API parameters one at a time, double-checks idempotency, verifies Kubernetes status, and reads the resulting VyOS config back over the API
- `scripts/lifecycle-api-matrix-vyos-vm.sh`
  - runs repeated lifecycle rounds, captures guest/config snapshots, executes the API matrix after guest reboot, hard reset, and stop/start transitions, and can chain the deeper smoke harness after each phase
- `scripts/live-fuzz-vyos-vm.sh`
  - runs randomized live CRD campaigns against the Venus cluster and VyOS VM, then double-checks convergence with a second controller pass per case
- `scripts/chaos-hbr-api-local.py`
  - local chaos harness for HBR API failure injection, including VyOS apply failures, status write failures, watch churn, deletion, pruning, and Kubernetes patch retries
- `scripts/boundary-hbr-api-local.py`
  - local boundary-value harness for edge VRF, route, BGP, netplan, and zero-command reconcile cases
- `scripts/fuzz-hbr-api-local.py`
  - local randomized fuzz harness that mutates supported HBR CRD fields and checks load/translate/reconcile/apply/idempotency invariants

## Examples

Plan commands from a CRD file:

```bash
python3 -m hbr_vyos_adapter.cli \
  plan --file examples/node-network-config.json
```

Apply commands to a VyOS node:

```bash
export VYOS_URL="https://vyos.example.test"
export VYOS_API_KEY="replace-with-real-api-key"

python3 -m hbr_vyos_adapter.cli apply \
  --file examples/node-network-config.json \
  --vyos-url "$VYOS_URL" \
  --api-key "$VYOS_API_KEY"
```

Track desired versus applied state without touching VyOS:

```bash
python3 -m hbr_vyos_adapter.cli reconcile \
  --file examples/node-network-config.json \
  --state-file /tmp/hbr-state.json
```

Track state and write a CRD-style status artifact:

```bash
python3 -m hbr_vyos_adapter.cli reconcile \
  --file examples/node-network-config.json \
  --state-file /tmp/hbr-state.json \
  --status-file /tmp/hbr-status.json
```

Render a status report from an existing state file:

```bash
python3 -m hbr_vyos_adapter.cli status \
  --state-file /tmp/hbr-state.json \
  --output /tmp/hbr-status.json
```

Preview the Kubernetes `status` patch without touching the API server:

```bash
python3 -m hbr_vyos_adapter.cli write-status \
  --state-file /tmp/hbr-state.json \
  --dry-run
```

Write status back to Kubernetes:

```bash
python3 -m hbr_vyos_adapter.cli write-status \
  --state-file /tmp/hbr-state.json \
  --kubeconfig ~/.kube/config
```

Client-certificate kubeconfigs such as the default RKE2 admin kubeconfig are
also supported.

Run the full controller loop once, including dry-run status writeback:

```bash
python3 -m hbr_vyos_adapter.cli controller \
  --file examples/node-network-config.json \
  --state-file /tmp/hbr-state.json \
  --status-file /tmp/hbr-status.json \
  --once \
  --write-status \
  --dry-run-status
```

Run the controller against Kubernetes CRDs with list/watch input:

```bash
python3 -m hbr_vyos_adapter.cli controller \
  --source kubernetes \
  --server https://kubernetes.default.svc \
  --state-file /tmp/hbr-state.json \
  --status-file /tmp/hbr-status.json \
  --max-iterations 2 \
  --deleted-retention-seconds 300 \
  --write-status \
  --dry-run-status
```

Reconcile and apply changed commands to VyOS:

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

If `PyYAML` is installed, YAML inputs are also supported.

## Assumptions

- HBR CRDs remain the source of truth in Kubernetes
- VyOS acts as a routing target, not as the CRD host itself
- translation favors explicit warnings over silent lossy behavior
