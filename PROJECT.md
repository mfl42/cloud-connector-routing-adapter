# Cloud Connector Routing Adapter Project

## Summary

This project explores how to use a routing target currently compatible with
VyOS for Sylva/Cloud Connector style Host Based Routing (HBR) workflows.

The first implementation deliberately uses an external Python adapter instead of
trying to embed HBR semantics directly inside the target NOS. The adapter reads
`NodeNetworkConfig` and `NodeNetplanConfig` style resources, translates a
supported subset into CLI/API operations for the current VyOS-compatible
target, and keeps unsupported semantics visible as warnings.

## Why This Project Exists

HBR and Cloud Connector are built around Kubernetes-native APIs and
host-oriented network lifecycle management. VyOS is strong at routing, policy,
VRFs, and appliance-style configuration management, but it does not natively
provide:

- HBR CRDs such as `NodeNetworkConfig` and `NodeNetplanConfig`
- the CRA integration contract expected by the HBR/network operator stack
- host-netplan style configuration semantics

At the same time, VyOS already provides enough networking features to act as a
real routing endpoint for part of that model:

- VRFs
- static routes
- policy routing
- BGP and FRR-based protocols
- HTTP API access
- Python-based internal command implementations

The project therefore aims to bridge the control model gap without rewriting
VyOS itself.

## Goals

- Accept HBR-like resource documents as input.
- Translate a safe, explicit subset into VyOS commands.
- Keep the translation transparent and inspectable.
- Make room for later integration with a Kubernetes controller/operator.
- Preserve a clean boundary between:
  - CRD ownership in Kubernetes
  - routing execution in VyOS

## Non-Goals

At this stage, the project does not try to:

- make VyOS itself host Kubernetes CRDs
- replace the full HBR operator
- fully emulate netplan behavior
- provide 100 percent CRA compatibility
- support every HBR object field on day one

## Current Architecture

The current scaffold is organized as:

- `hbr_vyos_adapter/models.py`
  - tolerant Python models for CRD-like documents
- `hbr_vyos_adapter/loader.py`
  - JSON/YAML loading
- `hbr_vyos_adapter/translator.py`
  - translation of supported resource fields into VyOS commands
- `hbr_vyos_adapter/reconcile.py`
  - desired-versus-applied comparison and apply orchestration
- `hbr_vyos_adapter/state.py`
  - persistent local revision/digest state store
- `hbr_vyos_adapter/status.py`
  - CRD-style status export shaped for future controller writeback
- `hbr_vyos_adapter/k8s_status.py`
  - Kubernetes status-subresource patch generation and writeback with bounded retries and deleted-object skips
- `hbr_vyos_adapter/k8s_documents.py`
  - Kubernetes CRD list/watch client for document ingestion with stale-watch recovery
- `hbr_vyos_adapter/controller.py`
  - loop that ties together document ingestion, selective reconcile, tombstone retention, status export, and status writeback
- `hbr_vyos_adapter/vyos_api.py`
  - minimal VyOS HTTP API client
- `hbr_vyos_adapter/cli.py`
  - local `plan`, `reconcile`, `status`, `write-status`, `controller`, and `apply` entrypoints

## Current Data Flow

1. Read `NodeNetworkConfig` or `NodeNetplanConfig`.
2. Normalize the document into tolerant Python models.
3. Translate supported fields into VyOS `set ...` commands.
4. Compute a desired revision/digest pair for reconcile visibility.
5. Compare that desired state with the locally recorded last-applied state.
6. Return:
   - generated commands
   - warnings
   - unsupported items
   - reconcile status
7. Optionally send commands to VyOS over the HTTP API.
8. Persist last-seen and last-applied state locally.
9. Optionally export a CRD-style status report JSON artifact.
10. Optionally patch the Kubernetes CRD `status` subresource from that report.
11. Optionally repeat that flow in a small controller loop backed by file polling or Kubernetes list/watch.
12. Reconcile only changed objects instead of reprocessing the whole snapshot every time.
13. Keep deleted objects as short-lived local tombstones so status/history remain inspectable without patching already removed CRDs.

## Supported Mapping Today

### `NodeNetworkConfig`

- VRF table creation
- interface-to-VRF attachment
- IPv4 and IPv6 static routes inside a specific routing table
- policy-route generation using:
  - interface
  - source prefixes
  - destination prefixes
  - source port
  - destination port
  - protocol
  - target VRF or target table
- basic VRF-scoped BGP neighbor generation using:
  - VRF local ASN/system-as
  - neighbor address
  - remote ASN
  - address-family activation
  - update-source
  - eBGP multihop

### `NodeNetplanConfig`

- interface IPv4/IPv6 addresses
- default routes
- ordinary static routes
- DNS name-server entries

## Unsupported or Partial Areas

The scaffold currently surfaces these explicitly instead of trying to fake full
support:

- `layer2s`
- CRA-specific L2/VNI behavior
- advanced FRR/BGP parity beyond the basic neighbor subset
- host readiness / rollout / reconciliation status
- full `NodeNetplanConfig` parity
- commit rollback and transactional diff logic

## Design Choice: External Adapter

The external adapter approach is the main architectural choice so far.

Advantages:

- avoids forcing Kubernetes CRD concerns into the VyOS appliance
- keeps the HBR API surface where it belongs: outside the router
- allows fast iteration in Python
- can target one or many VyOS instances
- makes translation traceable and testable

Disadvantages:

- adds another moving part
- requires explicit state/status mapping
- does not magically make VyOS “native HBR”

This tradeoff is acceptable for the first phase because it minimizes risk while
still proving functional alignment.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the full list of completed work and planned
improvements, including all known feature gaps.

## Open Questions

- How close does the adapter need to stay to the current Telekom/Sylva CRDs?
- Should unsupported fields fail hard or remain advisory warnings?
- Should BGP be translated directly into VyOS config, or should a smaller CRA
  compatibility layer be modeled first?
- How should rollback semantics be represented when VyOS commit/save behavior
  differs from host-netplan workflows?

## Practical Output of This Branch

This branch gives us:

- a dedicated work area for HBR/VyOS integration
- an initial adapter CLI
- example CRD-like inputs
- a documented starting architecture

That is enough to iterate on concrete field mappings instead of debating the
idea abstractly.
