# Validation Report

This document is the consolidated validation ledger for the Cloud Connector
routing adapter. It complements the usage-oriented guides by answering four
questions:

- what was tested
- what passed
- what failed during development and was corrected
- what is still outside the current stability claim

The current report reflects the branch state as of March 20, 2026.

## Scope Of The Stability Claim

The adapter is considered stable for the implemented subset described in
[MAPPING.md](MAPPING.md), with the following operating model:

- Kubernetes CRDs remain the source of truth
- the adapter translates a safe HBR subset into target CLI commands for the
  current VyOS-compatible profile
- the current target is configured through the VyOS HTTPS API
- unsupported semantics are surfaced as warnings or unsupported markers

This is not yet a claim of:

- full CRA compatibility
- full `NodeNetworkConfig` parity
- full `NodeNetplanConfig` parity
- full HBR delete convergence inside the controller without harness assistance
- guaranteed compatibility with all VyOS releases

## Validation Inventory

The adapter is validated through six layers:

1. Fast local regression
2. Local boundary tests
3. Local chaos tests
4. Local fuzzing
5. Live smoke and lifecycle testing on the VyOS VM
6. Live fuzzing against the Kubernetes cluster plus VyOS VM

Primary runbooks:

- [STABLE.md](STABLE.md)
- [SMOKE.md](SMOKE.md)
- [BOUNDARY.md](BOUNDARY.md)
- [CHAOS.md](CHAOS.md)
- [FUZZING.md](FUZZING.md)

## Executed Campaigns

### Fast Regression

Command:

```bash
scripts/check_examples.sh
```

Intent:

- verify translation snapshots
- verify reconcile/state/status flows
- verify Kubernetes source and status writeback logic
- run local chaos, boundary, and fuzz harnesses as part of the regression layer

Result:

- passing on the current branch state

### Boundary Campaign

Evidence:

- `artifacts/public/hbr-boundary-local-summary.json`

Observed result:

- `7` scenarios passed
- interface-family edges exercised
- static/policy-route boundaries exercised
- BGP boundary inputs exercised
- netplan route edges exercised
- invalid value boundaries exercised
- zero-command reconcile boundary exercised
- malformed structure boundaries exercised

### Chaos Campaign

Evidence:

- `artifacts/public/hbr-chaos-local-summary.json`

Observed result:

- `4` scenarios passed
- VyOS timeout and recovery path passed
- Kubernetes status conflict and recovery path passed
- watch churn, deletion, and prune path passed
- Kubernetes patch retry path passed

### Local Fuzz Campaign

Evidence:

- `artifacts/public/hbr-fuzz-local-summary.json`

Observed result from the latest recorded summary:

- `120` randomized cases passed
- `0` crashes
- `4541` commands exercised
- `685` warnings observed
- `316` unsupported markers observed
- every case converged after apply
- every repeat pass produced `0` pending commands

The local fuzz layer is intentionally broader than the live fuzz layer and
includes supported and unsupported-but-well-formed combinations.

### Live Smoke And Lifecycle Campaigns

Runbooks:

- [SMOKE.md](SMOKE.md)
- [CHAOS.md](CHAOS.md)

Observed result:

- live smoke passed against the VyOS VM
- deeper smoke passed in repeated campaigns
- lifecycle chaos passed through `baseline`, `reboot`, `reset`, and
  `stop-start` phases
- the adapter recovered after graceful reboot, hard reset, and full stop/start

Previously established live result:

- `2` lifecycle rounds completed successfully
- each phase executed the API parameter matrix and deeper smoke harness

Current rerun status on March 20, 2026:

- round `1` completed through `baseline`, `reboot`, `reset`, and `stop-start`
- round `2` completed `baseline`
- round `2` progressed through later phases during extended reruns, but the
  controlling SSH session was reset before a clean two-round completion could
  be recorded
- two regressions found during this rerun were corrected in the harness:
  - failed Kubernetes API tunnel establishment was not terminating the run
  - API-matrix teardown relied on coarse recursive deletes and could leave
    policy/VRF objects behind

Evidence-backed conclusion from the latest code:

- one full lifecycle round completed end-to-end on the patched branch
- the known tunnel and teardown regressions were both reproduced earlier and
  then verified as corrected
- the remaining instability observed in the extended two-round run was host
  transport interruption (`ssh` connection reset), not a reproduced adapter
  correctness failure

### Live Fuzz Campaign

Evidence:

- `artifacts/private/hbr-live-fuzz/summary.json`

Observed result from the current top-level summary:

- `1` seed
- `5` cases
- `5` live randomized cases passed
- every case performed a real first-pass apply
- every second pass produced:
  - `second_pending_command_count = 0`
  - `second_apply_performed = false`

Additionally, a deeper `2 x 10` live campaign was completed earlier during
development and was used to harden the live harness before this fresh rerun.
The current top-level summary now reflects the latest rerun after the summary
aggregation fix.

## Failures Found And Corrected

The following issues were discovered during live and fuzz campaigns and then
corrected in the adapter or test harness:

### 1. Invalid IPv6 Prefix Generation In Live Fuzz

Symptom:

- generated live cases produced non-aligned IPv6 prefixes

Correction:

- live generator now normalizes prefixes through proper IPv6 network masking

### 2. Unsafe Or Incomplete Live Cleanup Between Cases

Symptom:

- stale `policy route`, `policy route6`, `vrf`, or extra nameserver objects
  could leak across live cases

Correction:

- explicit pre/post case cleanup was added for live fuzz campaigns

### 3. Invalid Or Low VRF Table Selection

Symptom:

- generated tables could fall into ranges rejected by the real VyOS target or
  overlap in unhelpful ways during repeated campaigns

Correction:

- live fuzz now uses deterministic high table ranges per seed/case

### 4. Unsupported Live Inputs In The Management-Safe Profile

Symptom:

- the live profile was initially too broad and included combinations that were
  valid for local fuzzing but not appropriate for the real management-safe lab

Examples:

- IPv6 nameservers in the live safe profile
- `layer2s` generation in the live profile
- live BGP peers in combinations that were too unstable for the safe subset

Correction:

- the live fuzz profile was narrowed to the stable supported subset
- broader combinations remain covered by local fuzzing

### 5. Invalid Protocol/Port Combinations

Symptom:

- `icmp` traffic matches were being generated with ports

Correction:

- port generation is now constrained to protocol-safe combinations

### 6. Incorrect Policy Route Targeting For The Real VyOS API

Symptom:

- live fuzz exposed real VyOS API rejection on policy routes using
  `set table` in cases where the stable live profile should target a VRF

Correction:

- live profile now maps policy actions to `set vrf`

This was a key live-only correction and materially improved convergence on the
real device.

### 7. Live Fuzz Summary Aggregation Counted Stale Cases

Symptom:

- a smaller rerun could succeed but still publish a top-level `summary.json`
  containing older cases from previous larger runs

Correction:

- the summary writer now enumerates exactly the requested `seed/case` range for
  the current run instead of globbing every historical case directory

### 8. Revision-Only Reconcile Forced Unnecessary VyOS Apply Behavior

Symptom:

- when only the HBR revision changed and the generated command digest stayed the
  same, reconcile still treated the document as needing command re-apply

Correction:

- reconcile now distinguishes revision drift from command drift
- revision-only updates can advance applied state without issuing redundant
  VyOS API calls

### 9. State Persistence Was Not Atomic

Symptom:

- local reconcile state writes were direct in-place writes, which made the
  state file easier to corrupt on interruption

Correction:

- state is now written through a temporary file and atomically replaced

### 10. Kubernetes API Tunnel Failure Could Be Missed

Symptom:

- live runs could continue after a failed local `ssh -L` setup and only fail
  later when the controller tried to reach `127.0.0.1:<port>`

Correction:

- live harnesses now:
  - choose a free local tunnel port when the requested one is busy
  - rewrite the local kubeconfig when the port changes
  - fail fast if the SSH forward does not stay up

Affected scripts:

- `api-parameter-matrix-vyos-vm.sh`
- `deeper-smoke-vyos-vm.sh`
- `live-fuzz-vyos-vm.sh`

### 11. API-Matrix Teardown Was Not Deterministic Enough

Symptom:

- after successful apply/delete cycles, the matrix harness could leave policy
  and VRF state behind, causing false failures during cleanup verification

Correction:

- the API matrix now records explicit cleanup metadata per case and deletes:
  - policy rule subpaths
  - policy interface bindings
  - static route subpaths
  - BGP neighbor/system-as subpaths
  - VRF table/root paths
  - extra nameservers

## Current Evidence-Based Guarantees

For the currently implemented subset, the validation evidence supports these
claims:

- the adapter can load, translate, reconcile, and status supported HBR-like
  CRDs without crashing in the tested campaigns
- successful applies converge to a stable second-pass `noop`
- Kubernetes-backed sourcing and status writeback recover from tested stale
  watch and retryable patch failures
- the controller tolerates VM reboot, reset, and stop/start cycles in the live
  lab
- the live fuzz stable subset converges on the real VyOS VM

## Residual Gaps

These are the most important remaining gaps:

- deletion convergence on VyOS is not yet the strongest part of the design
- translation still emits raw CLI strings rather than a richer internal command
  model
- model parsing is permissive and does not yet provide strict schema validation
- the controller is a custom list/watch loop, not a full informer/controller
  framework
- the stability claim is based on one VyOS lab target profile, not a release
  compatibility matrix

## Recommended Next Validation Steps

If we want to deepen confidence further, the best next campaigns are:

1. Longer local fuzzing with multiple seeds and higher iteration counts
2. Longer live fuzz soak campaigns, for example `SEEDS=5 CASES_PER_SEED=20`
3. Explicit delete-convergence campaigns that verify native cleanup on VyOS
4. Version-compatibility runs against another VyOS build
5. Resource-pressure campaigns on reduced VM profiles

## Artifact Index

Current validation artifacts referenced by this report:

- `artifacts/public/hbr-boundary-local-summary.json`
- `artifacts/public/hbr-chaos-local-summary.json`
- `artifacts/public/hbr-fuzz-local-summary.json`
- `artifacts/private/hbr-boundary-local`
- `artifacts/private/hbr-chaos-local`
- `artifacts/private/hbr-fuzz-local`
- `artifacts/private/hbr-live-fuzz`
