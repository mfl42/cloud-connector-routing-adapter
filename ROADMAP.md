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

### P0 — Active bugs

#### translator.py — IndexError on empty port list

`traffic.source_ports[0]` and `traffic.destination_ports[0]` are accessed
without a prior length check. `models.py` can return an empty list from
`_string_list()`. If a policy route carries an empty ports list this raises
`IndexError` and halts translation for the entire document.

#### k8s_documents.py — uncaught JSONDecodeError in watch loop

`json.loads(line)` in `_watch_resource` has no try/except. A single malformed
line in the Kubernetes watch stream raises `JSONDecodeError` and crashes the
watch for that resource kind until the controller restarts.

#### state.py — mkdir not called for relative subdirectory paths

`save()` guards directory creation with
`if state_path.parent != Path(".")`. A path like `./subdir/state.json` has
`parent == Path(".")` so the condition is false and `mkdir()` is never called.
Writing the temporary file then raises `FileNotFoundError`.

#### k8s_status.py — TLS credential tempfiles never deleted

`_materialize_temp_file()` creates temporary files for certificates and keys
with `delete=False`. These files are never removed. On long-running deployments
or clusters with many nodes this leaks credential files on disk indefinitely.

#### reconcile.py — applied state updated on partial apply failure

When `client.configure_commands()` returns `{"success": false, ...}` the
reconcile loop still writes `applied_revision`, `applied_digest`, and
`applied_commands` to state. The local state diverges from the actual VyOS
configuration and subsequent reconcile cycles produce no diff, leaving the
target permanently misconfigured.

---

### P1 — Silent incorrect behaviour

#### translator.py — policy route emitted without protocol filter on unknown protocol

When `trafficMatch.protocols` contains only unrecognised values (e.g. `gre`,
`esp`), `_first_protocol()` returns `None` and no protocol clause is emitted.
The resulting policy rule matches all protocols on the specified interface,
which can intercept unintended traffic.

#### translator.py — ebgp-multihop 0 sent to VyOS

`BgpPeer.ebgp_multihop` is emitted if `is not None`. A value of `0` generates
`set ... ebgp-multihop '0'` which VyOS rejects at runtime, silently leaving
the peer without a multihop setting after a failed commit.

#### translator.py — policy-route IPv4 assumed when no valid prefix found

`_policy_address_family()` returns `4` when no valid prefix is found in the
source or destination lists. A policy route with only invalid or absent
prefixes is silently treated as an IPv4 rule rather than being skipped.

#### translator.py — incorrect VyOS interface type for VLAN on non-ethernet base

`_netplan_address_command()` falls back to `"ethernet"` when
`_infer_interface_type()` returns `None` for an unknown base interface (e.g.
`vxlan1.100`). The generated command targets `interfaces ethernet` instead of
the correct type, producing no visible error but configuring nothing.

#### translator.py — spurious "no table" warning for interface-only VRFs

The warning `vrf has no table; route programming may be incomplete` is emitted
for every VRF that has no `table` field, including VRFs that only carry
interface attachments where the table is not required.

#### controller.py — raw dict comparison causes false-positive reconcile triggers

`FileDocumentSource.wait_for_update()` compares documents using
`document.raw != stored.raw`. Two semantically identical JSON objects with
different key ordering are considered different. This triggers a reconcile cycle
with no effective changes.

#### controller.py — teardown exception aborts the reconcile cycle

`teardown_documents()` is called outside any exception guard. A VyOS API error
during teardown propagates and aborts the remainder of the reconcile iteration,
leaving active documents unprocessed.

#### state.py — corrupted state file raises unhandled exception

`ReconcileState.load()` returns an empty state if the file is absent, but
raises an unhandled `json.JSONDecodeError` if the file exists and is corrupted
(e.g. truncated after a crash). The adapter cannot start until the file is
manually removed.

#### state.py — non-numeric field value in state file raises ValueError

`_int_or_none()` calls `int(value)` without a try/except. A state file with a
non-numeric string in an integer field (e.g. after manual editing) raises
`ValueError` and prevents the adapter from loading any saved state.

#### status.py — transition_time changes on every call when timestamps are absent

`build_status_report()` falls back to `_utc_now()` when both `last_applied_at`
and `last_seen_at` are `None`. Two successive calls to the same function on the
same unchanged state produce different `transition_time` values in the exported
status, which may confuse downstream consumers.

#### reconcile.py — BGP delete consolidation regex misses quoted special characters

The regex `r"^set (vrf name '[^']+' protocols bgp neighbor '[^']+')(?:\s|$)"`
does not handle VRF names or neighbor addresses that contain an escaped single
quote. Such commands are not consolidated and instead generate individual leaf
deletes, which may leave VyOS with a partially configured neighbor after commit.

---

### P2 — Unprotected edge cases

#### translator.py — loopback inferred but never emitted

`_infer_interface_type()` returns `"loopback"` for interfaces starting with
`lo`. `"loopback"` is not in `_vrf_interface_types` so it always produces an
`unsupported` marker. The inferred type is misleading and could be replaced
with a direct unsupported marker.

#### models.py — empty VRF name accepted

`VrfSpec.from_dict()` does not validate that `name` is non-empty. An empty
string produces `set vrf name '' ...` commands that VyOS rejects at runtime.

#### vyos_api.py — idempotent error detection is version-dependent

`_is_idempotent_response()` matches on the literal strings `"already exists"`
and `"is already defined"`. VyOS error messages vary across versions. A future
version change could silently stop treating repeated applies as idempotent or,
conversely, treat a real error as idempotent.

#### vyos_api.py — show_config() is dead code

`VyosApiClient.show_config()` is defined but never called anywhere in the
adapter. It is either an unused stub or a missing call site.

#### k8s_documents.py — futures_wait timeout is double-counted

`futures_wait(timeout=full_timeout + self.timeout + 5)` adds the HTTP client
timeout on top of the watch timeout. With default values this is
`30 + 30 + 5 = 65` seconds. Under normal operation the watchers return well
within `full_timeout`; the added headroom is larger than necessary and delays
detection of hung threads.

#### k8s_documents.py — only the first watch event per cycle is processed

`_watch_resource()` returns immediately after the first `ADDED`, `MODIFIED`,
or `DELETED` event. If two CRDs change in rapid succession, the second event
is not seen until the next watch cycle, increasing convergence lag.

#### k8s_status.py — token file read without error handling

`Path(user_entry["tokenFile"]).read_text()` raises an unhandled exception if
the token file is deleted or becomes unreadable between kubeconfig parsing and
API use.

#### controller.py — local import inside conditional block

`from .status import write_status_report` is placed inside a conditional `if`
block (the tombstone pruning path). An `ImportError` in this module would only
be discovered at runtime when the specific condition is met, not at startup.

---

### P3 — Design fragility

#### models.py / translator.py — validation boundary is split across two layers

`models.py` accepts empty prefixes, missing next-hop addresses, and empty VRF
names. `translator.py` re-validates all of these and emits warnings. The
responsibility for input validation is duplicated and the adapter's actual
acceptance boundary is only visible by reading both files together.

#### models.py — protocol normalisation coupled to translator internals

Protocols are normalised to lowercase with hyphens replaced by underscores
(`tcp-udp` → `tcp_udp`) in `models.py`. `_first_protocol()` in `translator.py`
expects exactly this form. The coupling is implicit and undocumented; a change
to one side breaks the other without a type error.

#### k8s_resources.py — CRD plural names are hardcoded

`kind_to_plural()` contains two hardcoded entries. Adding support for a new
CRD kind requires modifying this function directly. There is no registration
mechanism or configuration path.

---

## Planned

Gaps are grouped by theme. Each entry names the affected field or component,
describes the current behaviour, and states what is missing.

---

### Translation gaps — NodeNetworkConfig

#### Policy route: direct next-hop action not emitted

`policyRoute.nextHop.address` is parsed and surfaced as a warning but generates
no VyOS command. The adapter currently maps policy rules only to a target VRF
or routing table (`set vrf` / `set table`). A direct next-hop action
(`set nexthop`) is not yet emitted.

Current behaviour: `nextHop.address` present → warning, no command.

#### Policy route: only the first protocol is used

`trafficMatch.protocols` is a list but only the first recognised protocol is
translated. Subsequent entries are silently dropped.

Current behaviour: `["tcp", "udp"]` → only `tcp` is emitted.

#### Policy route: only the first port is used

`trafficMatch.sourcePorts` and `trafficMatch.destinationPorts` are lists but
only index `[0]` is translated. Multiple port values or port ranges are
silently dropped.

#### Static route: next-hop interface not mapped

`staticRoute.nextHop` only reads `address`. A next-hop interface binding
(`nextHop.interface` or equivalent) is not translated.

#### BGP: peer password not mapped

`password` / `peerPassword` on a BGP peer entry is not in the supported field
set. It is silently absent from the generated commands.

#### BGP: route-map and prefix-list not mapped

Route policies (`routeMap`, `inboundRouteMap`, `outboundRouteMap`), prefix
filters (`prefixList`, `distributionList`), and community attributes are not
in the supported peer field set. They are surfaced as unsupported fields on
the peer.

#### BGP: timers not mapped

`keepalive`, `holdtime`, and similar timer fields on BGP peers are not
translated.

#### BGP: BFD and graceful-restart not mapped

`bfd` and `gracefulRestart` peer-level flags are not in the supported field
set. They are surfaced as unsupported fields on the peer.

#### BGP: global router-id not mapped

There is no path in the current translation to emit
`set vrf name 'X' protocols bgp parameters router-id`. Router-id is not read
from the VRF spec.

---

### Translation gaps — NodeNetplanConfig

#### Interface family assumed to be ethernet

`NodeNetplanConfig` address and route translation unconditionally emits
`set interfaces ethernet <name> ...`. Bond (`bond*`), bridge (`br*`), dummy
(`dum*`), and other non-ethernet families declared in netplan are not
disambiguated. The correct VyOS interface type would not be selected.

#### Interface MTU not mapped

`<interface>.mtu` is present in netplan semantics but is not read from the
model or translated to `set interfaces <family> <name> mtu '<value>'`.

#### Interface route metric not mapped

`route.metric` is not read from `RouteConfig`. VyOS supports
`set protocols static route '<prefix>' next-hop '<via>' distance '<metric>'`.
All routes are currently applied without a metric.

#### Interface route on-link flag not mapped

`route.on-link` (used in netplan to suppress next-hop reachability checks) is
not translated. VyOS does not have a direct equivalent but the intent needs
explicit handling.

#### DHCP / dynamic address not mapped

`dhcp4` and `dhcp6` interface flags are not read from the model. Dynamic
address assignment via `set interfaces <family> <name> address dhcp` is not
emitted.

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

#### No delete emitted for policy route interface binding on document change

`set policy route '<name>' interface '<if>'` is emitted once per policy route.
If the bound interface changes in a document update, the old binding is not
explicitly removed before the new one is written. VyOS may retain both bindings
depending on version behaviour.

#### No delete emitted for VRF table change

If `<vrf>.table` changes between two revisions, the adapter emits the new
`set vrf name '<name>' table '<new>'` but does not delete the old table
assignment. On some VyOS versions this produces a validation error.

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
