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

## Known Bugs

Bugs identified by static analysis of the current codebase. Grouped by
severity: P0 (crash or guaranteed state corruption), P1 (silent incorrect
behaviour), P2 (unprotected edge case), P3 (design fragility).

---

### P0 — Fixed

#### ~~translator.py — IndexError on empty port list~~ (false positive)

On analysis the access `traffic.source_ports[0]` is already guarded by
`if traffic.source_ports:` on the preceding line. No fix required.

#### k8s_documents.py — uncaught JSONDecodeError in watch loop ✓

`json.loads(line)` in `_watch_resource` now wraps the parse in a
try/except JSONDecodeError. Malformed lines are skipped; the watch continues.

#### state.py — mkdir not called for relative subdirectory paths ✓

The condition `if state_path.parent != Path(".")` was removed. `mkdir()` is
now called unconditionally with `exist_ok=True`, which is a no-op when the
directory already exists and correctly handles all path forms including
relative subdirectory paths.

#### k8s_status.py — TLS credential tempfiles never deleted ✓

`_materialize_temp_file()` now appends each created path to a module-level
list. An `atexit` handler calls `os.unlink()` on all tracked paths at process
exit. Files that were already removed are silently ignored.

#### reconcile.py — applied state updated on partial apply failure ✓

The apply section now reads `vyos_response.get("success", True)` before
updating state. On failure: `last_result` is set to `"apply-failed"`,
`last_error` records the VyOS error message, and `applied_revision`,
`applied_digest`, and `applied_commands` are left unchanged. The document
action in the run result is set to `"apply-failed"` so callers can distinguish
a failed apply from a pending-apply or no-op.

---

### P1 — Fixed

#### ~~translator.py — policy route emitted without protocol filter on unknown protocol~~ ✓

When all protocols in `trafficMatch.protocols` are unrecognised, the rule is
now skipped entirely. Previously the rule was emitted without a protocol
clause, matching all traffic on the interface.

#### ~~translator.py — ebgp-multihop 0 sent to VyOS~~ ✓

`ebgp-multihop` is now only emitted when the value is greater than zero.
A value of `0` (disabled) is silently skipped.

#### ~~translator.py — policy-route IPv4 assumed when no valid prefix found~~ ✓

`_policy_address_family()` now returns `None` (skip rule) when prefixes are
provided but all are invalid. The IPv4 default is only used when no prefixes
are specified at all.

#### ~~translator.py — incorrect VyOS interface type for VLAN on non-ethernet base~~ ✓

`_netplan_interface_path()` (replacing `_netplan_address_command()`) now uses
`_infer_interface_type()` with an `"ethernet"` fallback only, and applies the
same logic for both VLAN sub-interfaces and plain interfaces. All address,
MTU, DHCP, and route commands derive their type from the same helper.

#### ~~translator.py — spurious "no table" warning for interface-only VRFs~~ ✓

The warning is now scoped to VRFs that have static routes, BGP peers, or
policy routes — the cases where the routing table is actually required.

#### ~~controller.py — raw dict comparison causes false-positive reconcile triggers~~ ✓

Document change detection now uses `json.dumps(sort_keys=True)` for
comparison, making it insensitive to key ordering differences.

#### ~~controller.py — teardown exception aborts the reconcile cycle~~ ✓

`teardown_documents()` is now wrapped in a try/except. Teardown failures are
logged to stderr but do not abort the reconcile iteration.

#### ~~state.py — corrupted state file raises unhandled exception~~ ✓

`ReconcileState.load()` now catches `json.JSONDecodeError` and returns an
empty state, allowing the adapter to start cleanly after a crash.

#### ~~state.py — non-numeric field value in state file raises ValueError~~ ✓

`_int_or_none()` now catches `ValueError` and `TypeError`, returning `None`
for any non-numeric value instead of crashing.

#### ~~status.py — transition_time changes on every call when timestamps are absent~~ ✓

`build_status_report()` now accepts an optional `now` parameter and threads a
single timestamp through all `_build_document_status()` calls. Successive
calls on the same unchanged state produce identical output.

#### reconcile.py — BGP delete consolidation regex misses quoted special characters

The regex `r"^set (vrf name '[^']+' protocols bgp neighbor '[^']+')(?:\s|$)"`
does not handle VRF names or neighbor addresses that contain an escaped single
quote. Such commands are not consolidated and instead generate individual leaf
deletes, which may leave VyOS with a partially configured neighbor after commit.

---

### P2 — Fixed

#### ~~translator.py — loopback inferred but never emitted~~ ✓

`"loopback"` has been removed from `_infer_interface_type()`. Interfaces
starting with `lo` now produce a clean "unknown interface family" unsupported
marker rather than the misleading "inferred as loopback" message.

#### ~~models.py — empty VRF name accepted~~ ✓

`VrfSpec.from_dict()` now raises `ValueError` immediately when `name` is empty
or whitespace-only, making the models layer the primary rejection point.

#### ~~vyos_api.py — idempotent error detection is version-dependent~~ ✓

`_is_idempotent_response()` now also matches `"already set"` and
`"is already present"` to cover additional VyOS version variants.

#### ~~vyos_api.py — show_config() is dead code~~ ✓

`VyosApiClient.show_config()` has been removed.

#### ~~k8s_documents.py — futures_wait timeout is double-counted~~ ✓

The outer `futures_wait` timeout is now `full_timeout + self.timeout`,
removing the redundant `+5` constant.

#### ~~k8s_documents.py — only the first watch event per cycle is processed~~ ✓

`_watch_resource()` now collects all events from the stream and returns a
`list[WatchEvent]`. After the first change event, a 150 ms drain timer closes
the stream so that events arriving in rapid succession are all captured in one
cycle without blocking for the full `timeoutSeconds`. Callers updated to use
`extend` instead of a single-event append.

#### ~~k8s_status.py — token file read without error handling~~ ✓

The `tokenFile` read is now wrapped in a try/except that surfaces a clear
`RuntimeError` instead of letting an `OSError` propagate unexpectedly.

#### ~~controller.py — local import inside conditional block~~ ✓

`write_status_report` is now imported at module level alongside the other
`status` imports.

---

### P3 — Fixed

#### ~~models.py / translator.py — validation boundary is split across two layers~~ ✓

`VrfSpec.from_dict()` now raises `ValueError` immediately for an empty VRF
name, making the models layer the primary rejection point for that case.
Remaining translator-side guards are kept as a defence-in-depth layer for
objects constructed directly (not via `from_dict`).

#### ~~models.py — protocol normalisation coupled to translator internals~~ ✓

`translator.py` now exposes `_SUPPORTED_PROTOCOLS` as a module-level
`frozenset` with an explicit comment documenting the coupling to the
`models.py` normalisation step. A cross-reference comment in `models.py`
points back to `_SUPPORTED_PROTOCOLS`, making the contract visible without
reading both files in full.

#### ~~k8s_resources.py — CRD plural names are hardcoded~~ ✓

`SUPPORTED_CUSTOM_RESOURCES` is now a mutable list and `register_resource()`
is exposed as a public function. Callers can add or replace a CRD kind at
runtime without modifying `k8s_resources.py`.

---

## Planned

Gaps are grouped by theme. Each entry names the affected field or component,
describes the current behaviour, and states what is missing.

---

### Translation gaps — NodeNetworkConfig

#### ~~Policy route: direct next-hop action not emitted~~ ✓

`policyRoute.nextHop.address` is now emitted as
`set policy route '<name>' rule '<id>' set nexthop '<address>'`.
Address takes priority over `vrf` and `table` targets.
Address-family mismatch between the rule and the nexthop produces a warning
and skips the nexthop command.

#### ~~Policy route: only the first protocol is used~~ ✓

`_translate_policy_route()` now iterates over all supported protocols and emits
one VyOS rule per protocol (incrementing rule_id). Unsupported protocols are
skipped; if all are unsupported the rule is omitted with a warning.

#### ~~Policy route: only the first port is used~~ ✓

`sourcePorts` and `destinationPorts` are now joined with commas
(`",".join(...)`) and emitted as a single VyOS port value, which VyOS
natively accepts as a comma-separated list.

#### ~~Static route: next-hop interface not mapped~~ ✓

`NextHop` gains an `interface` field (parsed from `interface`, `dev`, or
`outboundInterface`). `_translate_static_route` now emits
`set ... static route '<prefix>' interface '<iface>'` when only an interface
is set, or `next-hop '<addr>'` when an address is set. Both fields are
accepted; address takes priority when both are present.

#### ~~BGP: peer password not mapped~~ ✓

`password` / `peerPassword` / `bgpPassword` are now parsed into `BgpPeer.password`
and emitted as `set ... neighbor '<addr>' password '<value>'`.

#### BGP: route-map / import-export filter compilation not implemented

The upstream CRD defines per-address-family `importFilter` and `exportFilter`
objects (with `defaultAction`, `items`, prefix matchers, community modifiers).
Simple string variants (`routeMap`, `prefixList`, `distributionList`) are also
handled.

Current behaviour: all filter/route-map field names are recognised (no longer
surfaced as "unknown fields") and emitted as a dedicated unsupported marker:
`has route-map/filter fields (...); route-map compilation not yet supported`.

What is missing: compilation of filter specs into VyOS `set policy route-map`
objects and binding them to the BGP neighbor per address-family. This requires
a new translation stage (route-map object registry + per-rule emit) and is a
planned but non-trivial addition.

#### ~~BGP: timers not mapped~~ ✓

`keepalive` and `holdtime` are parsed into `BgpPeer.keepalive` / `BgpPeer.holdtime`
(also via a nested `timers` dict) and emitted together as
`set ... timers keepalive` / `set ... timers holdtime`. If only one is present
a warning is emitted and both are skipped (VyOS requires both to be set together).

#### ~~BGP: BFD and graceful-restart not mapped~~ ✓

`bfd` and `gracefulRestart` / `graceful-restart` are parsed into `BgpPeer.bfd`
and `BgpPeer.graceful_restart` and emitted as leaf `set ... bfd` /
`set ... graceful-restart` nodes.

#### ~~BGP: global router-id not mapped~~ ✓

`routerId` / `router-id` / `bgpRouterId` are parsed into `VrfSpec.bgp_router_id`
and emitted as `set vrf name '<n>' protocols bgp parameters router-id '<id>'`.

---

### Translation gaps — NodeNetplanConfig

#### ~~Interface family assumed to be ethernet~~ ✓

`_netplan_interface_path()` now calls `_infer_interface_type()` for both the
base interface and VLAN sub-interfaces. All address, MTU, and DHCP commands
use the inferred type (`bonding`, `bridge`, `dummy`, `wireguard`, etc.),
falling back to `ethernet` only when the prefix is unrecognised.

#### ~~Interface MTU not mapped~~ ✓

`InterfaceConfig.mtu` is parsed from the spec dict and emitted as
`set interfaces <family> <name> mtu '<value>'`.

#### ~~Interface route metric not mapped~~ ✓

`RouteConfig.metric` is parsed and, when present, appended as
`distance '<metric>'` to the static route next-hop command.

#### Interface route on-link flag not mapped

`route.on-link` (used in netplan to suppress next-hop reachability checks) is
not translated. VyOS does not have a direct equivalent but the intent needs
explicit handling.

#### ~~DHCP / dynamic address not mapped~~ ✓

`InterfaceConfig.dhcp4` and `InterfaceConfig.dhcp6` are parsed from the spec
dict and emitted as `set interfaces <family> <name> address 'dhcp'` /
`set interfaces <family> <name> address 'dhcp6'`.

---

### Convergence gaps

#### Rollback on apply failure

When a `/configure-list` call is accepted by VyOS but the subsequent commit
fails, the adapter has no mechanism to roll back staged changes. A subsequent
apply may encounter a partially staged configuration.

Current behaviour: apply error is propagated; state is not updated; no
rollback is attempted.

Planned: invoke `rollback` via the VyOS API on detected commit failure, then
re-attempt from a clean baseline.

#### ~~No delete emitted for policy route interface binding on document change~~ ✓

`set policy route '<name>' interface '<if>'` is included in `applied_commands`.
When the interface changes, `_compute_diff_deletes` emits
`delete policy route '<name>' interface '<old>'` before the new set.
The value is correctly kept in the delete path because `interface` is a
list-key node in VyOS (not a scalar leaf).

#### ~~Scalar leaf delete includes value — VyOS rejects path~~ ✓

`_compute_diff_deletes()` now calls `_to_delete_path()` which strips the
trailing value from known scalar leaf nodes (`table`, `system-as`, `router-id`,
`remote-as`, `update-source`, `ebgp-multihop`, `password`, `timers keepalive`,
`timers holdtime`). VyOS requires the path without a value for these nodes.

#### ~~No delete emitted for VRF table change~~ ✓

When `table` changes, the old `set vrf name '<name>' table '<old>'` falls into
`removed_cmds`. `_to_delete_path` matches `_SCALAR_LEAF_RE` and emits
`delete vrf name '<name>' table` (value stripped, as VyOS requires), before
the new `set vrf name '<name>' table '<new>'` is applied.

---

### Status and operator contract gaps

#### Full CRA status contract not implemented

The adapter exports a local `AdapterStatusReport` and patches the Kubernetes
`status` subresource with phase, revision, warnings, and conditions. The full
CRA readiness, rollout, and reconciliation status fields expected by the HBR
operator are not yet modelled.

Current fields emitted: `phase`, `observedRevision`, `warningCount`,
`unsupportedCount`, `conditions` (DesiredSeen, Applied, InSync, HasWarnings,
Deleted).

Missing: rollout readiness gates, per-interface readiness, CRA-level
`reconciling`/`degraded`/`available` conditions.

#### `spec.revision` not forwarded to VyOS

`NodeNetworkConfig.spec.revision` is tracked for reconcile visibility and
surfaced as a translation warning. No VyOS config command is emitted for it.
This is by design today but means the routing target has no visibility of the
HBR revision currently applied.

---

### L2 / VNI

`spec.layer2s` in `NodeNetworkConfig` is detected and surfaced as unsupported.
No translation is attempted.

VXLAN interface creation (`set interfaces vxlan vxlan<n> ...`), VNI-to-VRF
binding, and EVPN address-family activation under BGP are all absent.

---

### Kubernetes integration gaps

#### Real informer loop

The current Kubernetes source uses a manual list-then-watch loop with
stale-watch recovery via relist. A shared-cache informer with event handlers
would reduce API server load and improve change detection latency.

#### Cluster-scoped CRD watch not exercised by default

The controller defaults to namespace-scoped list/watch. Cluster-scoped CRD
deployments require `--cluster-scoped` and are not covered by the local
regression harness.

#### No leader election

Running multiple adapter instances against the same Kubernetes namespace and
VyOS target produces conflicting applies. No leader-election or locking
mechanism is implemented.
