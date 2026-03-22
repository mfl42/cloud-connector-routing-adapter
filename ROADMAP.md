# Roadmap

## Objective

Support complex network configurations — multiple VRFs, VLAN subinterfaces,
BGP peers, policy routes, static routes — without throughput bottlenecks,
silent misconfiguration, or incomplete convergence on deletion.

## Completed

### Batch apply — VyOS API

**Problem:** each `set` command was sent as a separate HTTPS request to the
VyOS API. For a configuration involving 8 VRFs and VLAN subinterfaces, this
produced 100 or more sequential HTTP roundtrips, causing a throughput
bottleneck proportional to configuration complexity.

**Fix:** the adapter now sends the full batch of `set`/`delete` operations in
a single `/configure-list` call. The previous sequential path is kept as a
fallback for older VyOS targets that do not expose `/configure-list`.

Impact: N sequential roundtrips → 1 atomic transaction, regardless of the
number of VRFs, routes, or peers in the configuration.

---

### VLAN subinterface mapping

**Problem:** interface names in the form `eth0.100` were passed as-is to VyOS
`set` commands. VyOS does not accept dot-notation in that position — VLAN
subinterfaces are configured through the `vif` hierarchy
(`set interfaces ethernet eth0 vif 100 ...`). The resulting commands were
silently accepted by VyOS but produced no actual configuration.

**Fix:** the translator detects the `base.vlan-id` pattern and generates the
correct `vif` hierarchy for both VRF attachment and interface address
assignment. The fix covers `NodeNetworkConfig` (VRF attachment) and
`NodeNetplanConfig` (address assignment).

---

### Delete convergence — teardown on document removal

**Problem:** when a `NodeNetworkConfig` or `NodeNetplanConfig` was deleted from
Kubernetes, the adapter marked it as a local tombstone but sent no `delete`
commands to VyOS. The routing configuration remained in place on the target.

**Fix:** the controller now calls `teardown_documents` immediately after
marking a document as deleted. Teardown generates coarse-grained VyOS deletes:
one `delete vrf name 'X'` covers the entire VRF block (table, static routes,
BGP), and separate deletes handle policy route maps and interface VRF
attachments. Netplan-sourced configuration (addresses, nameservers, static
routes) is removed with fine-grained targeted deletes.

---

### Delete convergence — partial modification diff

**Problem:** when a document was modified to remove a VRF, a static route, or a
BGP peer, only the new desired `set` commands were sent. The removed items were
never deleted from VyOS.

**Fix:** on each apply, the adapter now computes the diff between the previously
applied command set and the new desired command set. Commands that were applied
before but are absent from the new state generate `delete` operations, prepended
before the new `set` batch. For BGP peers, all leaf-level removes for a given
neighbor are collapsed into a single `delete ... bgp neighbor 'addr'` to avoid
leaving VyOS in a partially configured state during commit.

---

### Applied command tracking

**Problem:** the local state file tracked revision digests but not the commands
themselves, making it impossible to compute a precise diff on modification or
reconstruct teardown commands on deletion.

**Fix:** `DocumentState` now persists `applied_commands` — the exact list of
`set` commands sent during the last successful apply. This field drives both
the diff-based partial delete and the full teardown path.

---

### Kubernetes watch — concurrent resource kinds

**Problem:** when watching multiple CRD kinds (NodeNetworkConfig and
NodeNetplanConfig), the adapter polled them sequentially and split the timeout
budget between them. With N kinds, each watch received only 1/N of the
available time before the loop moved on, increasing the lag between a change in
Kubernetes and the next reconcile cycle.

**Fix:** `watch_for_change` now launches one watcher thread per resource kind
and runs them concurrently via `ThreadPoolExecutor`. Each watcher receives the
full timeout. The single-kind path avoids thread overhead entirely.

---

### Idempotent error tolerance — sequential fallback

**Problem:** when the `/configure-list` batch endpoint was unavailable and the
adapter fell back to sequential single-command calls, a VyOS response of
`{"success": false, "error": "... already exists"}` caused the entire
operation to be reported as failed. This could block state progression on
re-apply of a partially applied configuration.

**Fix:** the sequential fallback now recognises "already exists" and
"is already defined" responses as idempotent successes. The overall operation
succeeds as long as every command either applied or was already present.

---

### File polling — mtime guard

**Problem:** `FileDocumentSource.wait_for_update` reloaded and reparsed the
source file on every poll interval, regardless of whether the file had changed.

**Fix:** the poll loop now reads `os.stat().st_mtime` before attempting a
reload. If the timestamp is unchanged, the poll returns immediately without
I/O or parsing.

---

### Translator lifecycle

**Problem:** `VyosTranslator` was instantiated on every controller iteration
inside the reconcile loop, allocating a new object with each pass even though
the translator is stateless.

**Fix:** `run_controller` instantiates `VyosTranslator` once at startup and
passes the same instance to every reconcile call.

---

## Planned

### Rollback on apply failure

When a `/configure-list` call succeeds partially before VyOS rejects the
commit, the adapter has no mechanism to roll back the staged changes. A
subsequent apply may encounter a VyOS in an inconsistent state.

Planned: invoke `rollback` via the VyOS API on detected commit failure, then
re-attempt from a clean baseline.

---

### BGP policy and advanced peer options

The current BGP translation covers `remote-as`, `update-source`, `ebgp-multihop`,
and address-family activation. Route policies, community attributes, prefix
filters, and timers are surfaced as unsupported warnings.

Planned: extend the BGP translator to cover the most common peer policy fields
used in Sylva/Cloud Connector deployments.

---

### Full CRA status contract

The adapter currently exports a local status report and patches the Kubernetes
`status` subresource. The full CRA readiness, rollout, and reconciliation
status contract is not yet implemented.

Planned: align the status export with the CRA contract fields expected by the
upstream HBR operator.

---

### L2 / VNI support

`layer2s` fields in `NodeNetworkConfig` are detected and surfaced as
unsupported warnings. No translation is attempted.

Planned: evaluate VXLAN and VNI mapping options for the current VyOS-compatible
target and implement a safe, explicit subset.

---

### Real Kubernetes informer loop

The current Kubernetes source relies on a list-then-watch loop with manual
stale-watch recovery. A proper informer with shared cache and event handlers
would reduce load on the API server and improve responsiveness.

Planned: evaluate replacement with a lightweight informer client once the core
translation and convergence layer is stable.
