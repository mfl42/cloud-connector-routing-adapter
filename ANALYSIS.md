# Problem Analysis And Proposed Solution

## Problem Statement

We want to understand whether a routing target currently compatible with VyOS
can participate in a Sylva/Cloud Connector style Host Based Routing
environment and, if so, what shape that integration should take.

The key question is not only whether the current VyOS-compatible target can
perform the routing functions, but whether it can fit the API and lifecycle
model expected by HBR.

## What HBR Expects

At a high level, the HBR/network-operator model expects:

- Kubernetes CRDs such as `NodeNetworkConfig` and `NodeNetplanConfig`
- a contract for a routing/configuration agent
- host-style reconfiguration semantics
- status/revision feedback
- progressive rollout behavior

This is not just a set of routes. It is a control model.

## What VyOS Already Provides

VyOS already gives us several relevant networking capabilities:

- VRFs
- policy routing
- static routing
- BGP and FRR-based protocols
- a Python-heavy internal implementation
- HTTP API access for automation

From a pure networking perspective, VyOS is strong enough to implement a large
part of the desired forwarding behavior.

## The Core Mismatch

The mismatch is architectural:

- HBR is Kubernetes-native and host-centric
- VyOS is appliance-centric and config-engine-driven

Examples of that mismatch:

- `NodeNetplanConfig` assumes host interface semantics, while VyOS has its own
  interface model and config engine
- CRA-style dynamic state and revision management do not directly exist as a
  first-class VyOS abstraction
- CRDs are not native resources inside VyOS

So the question is not “Can VyOS route traffic like HBR wants?”

The real question is:

“Where should the translation between the HBR API model and the VyOS appliance
model live?”

## Options Considered

### Option 1: Make VyOS Natively Host HBR Semantics

Meaning:

- implement CRD semantics directly inside VyOS
- make VyOS act more like an HBR-native node agent

Pros:

- tighter conceptual integration
- fewer external moving parts

Cons:

- high implementation cost
- wrong layer for Kubernetes-native APIs
- risks bending VyOS away from its appliance model
- much harder to upstream or maintain

### Option 2: External Adapter/Controller

Meaning:

- keep HBR CRDs in Kubernetes
- write a Python adapter that translates those resources into VyOS API/config
- optionally report status back

Pros:

- matches system boundaries better
- easier to prototype
- low-risk and incremental
- keeps VyOS modifications small or optional

Cons:

- requires explicit translation logic
- full status/contract fidelity must be implemented separately

### Option 3: Ignore HBR APIs And Use VyOS As A Manual Router

Pros:

- simplest in the short term

Cons:

- not API-compliant
- not useful for Cloud Connector/HBR integration goals

## Chosen Direction

Option 2 is the best first move:

- HBR remains the declarative API layer
- VyOS remains the routing target
- the adapter becomes the contract bridge

This gives the best balance between realism, speed, and maintainability.

## Why Python

Python is a good fit here because:

- VyOS itself already uses Python heavily
- CRD-like document translation is straightforward in Python
- rapid iteration matters more than early performance
- an adapter/controller can later be embedded in a larger operator stack if
  needed

## Minimal Viable Translation

The smallest useful bridge is:

- `NodeNetworkConfig`
  - VRFs
  - interface attachment
  - static routes
  - policy routes
  - basic BGP peers
- `NodeNetplanConfig`
  - interface addresses
  - routes
  - DNS servers

This already proves whether a meaningful subset of HBR intent can be rendered
onto VyOS.

The next minimal control-plane layer after translation is local reconcile
state:

- what revision we want
- what digest of commands that revision produced
- what revision/digest we last applied

That still falls short of full CRD status writeback, but it gives the adapter a
real notion of convergence instead of staying a stateless formatter.

The natural next extension after that is a status artifact shaped like the
future CRD writeback payload:

- phase
- desired revision
- applied revision
- command digest
- simple conditions such as `InSync` or `Applied`

That is useful even before a Kubernetes watcher exists because it forces the
adapter to make its convergence story explicit.

Once that artifact exists, the next practical step is a narrow Kubernetes
writer that patches only the CRD `status` subresource. That keeps the adapter
from mutating desired state while still letting it participate in the operator
feedback loop.

After that, a minimal controller loop can simply compose the pieces we already
have:

- load documents
- translate and reconcile
- export status
- optionally write status back
- optionally apply to VyOS

That is enough to prove the control flow before replacing file polling with a
real Kubernetes watch.

The next refinement after a file-backed controller is to let Kubernetes become
the document source as well, via CRD list/watch calls. That still is not a full
informer/cache implementation, but it moves the adapter much closer to the
shape of a real operator sidecar.

Once that path exists, the first hardening priorities are predictable:

- retry status writes on transient conflicts or API pressure
- recover from stale watch resource versions by relisting

Those two behaviors do not make the adapter a full operator, but they close two
of the most obvious gaps between a lab scaffold and a resilient control loop.

The next improvement after that is selective reprocessing:

- keep a cache of the last seen CRD objects
- update that cache from watch events
- reconcile only the objects that actually changed

That brings the runtime cost and behavior much closer to an informer-driven
controller, even before adding a full shared cache implementation.

The next control-plane detail after selective reprocessing is deletion
finalization:

- when a CRD disappears, keep a short-lived local tombstone
- mark its phase as `Deleted` in local status output
- avoid patching Kubernetes `status` for an object that no longer exists
- prune that tombstone after a retention window

That keeps the adapter debuggable without turning every delete into a noisy
writeback failure.

## What Must Remain Explicitly Unsupported

We should not hide gaps behind fake compatibility.

The adapter must clearly surface unsupported areas such as:

- `layer2s`
- VNI/L2 CRA semantics
- advanced BGP/EVPN parity
- exact netplan parity
- rollout orchestration
- detailed status and health contract

That explicitness is important because silent lossiness is worse than partial
support.

## Success Criteria

This project is successful if it can:

- consume realistic HBR-style documents
- generate sensible VyOS commands
- apply them safely through the VyOS API
- make unsupported areas visible
- evolve toward a proper external controller

It does not need to achieve total compatibility immediately to be valuable.

## Risks

- HBR CRD shapes may evolve
- status semantics may prove more complex than command translation
- some host-centric assumptions may not map cleanly to an appliance OS
- rollback/reconcile behavior may need a separate transaction model

## Mitigation Strategy

- keep models tolerant
- prefer additive mappings
- emit warnings for unsupported fields
- document assumptions
- keep the adapter separate from core VyOS changes

## Final Conclusion

VyOS is a plausible routing engine for HBR-style intents, but not a native HBR
node by itself.

The clean solution is an external adapter that translates:

- HBR desired state
- into VyOS configuration/API operations
- while preserving visibility into unsupported semantics

That is why this branch starts with an external Python adapter rather than a
deep internal VyOS rewrite.
