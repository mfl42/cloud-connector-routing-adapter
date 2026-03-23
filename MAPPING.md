# Cloud Connector Routing Adapter Field Mapping

This document captures the current field mapping implemented by the adapter for
the current VyOS-compatible target profile.

It is intentionally conservative: supported fields are mapped explicitly, and
everything else should remain either a warning or an unsupported item until it
is implemented deliberately.

## `NodeNetworkConfig`

### Document-Level Fields

- `spec.revision`
  - Current behavior:
    - tracked as a translation warning for visibility
    - used as `desired_revision` in reconcile state
  - VyOS output:
    - no direct config command (design decision — revision tracking stays on the Kubernetes side)

- `spec.layer2s`
  - Current behavior:
    - reported as unsupported
  - VyOS output:
    - none
  - Reason:
    - VXLAN/EVPN/VNI semantics deferred until upstream API stabilises

### VRF Blocks

Supported sources:

- `spec.clusterVRF`
- `spec.fabricVRFs.*`
- `spec.localVRFs.*`

For each VRF:

- `<vrf>.table`
  - VyOS:
    - `set vrf name '<name>' table '<table>'`

- `<vrf>.interfaces[]`
  - VyOS:
    - `set interfaces <family> <ifname> vrf '<name>'`
    - `set interfaces <family> <base> vif '<vid>' vrf '<name>'` (for `base.vid` VLAN notation)
  - Supported inferred families:
    - `eth*`, `en*` -> `ethernet`
    - `bond*` -> `bonding`
    - `br*` -> `bridge`
    - `pppoe*` -> `pppoe`
    - `dum*` -> `dummy`
    - `veth*` -> `virtual-ethernet`
    - `wg*` -> `wireguard`
    - `vti*` -> `vti`
    - `vxlan*` -> `vxlan`
  - Notes:
    - unknown families produce an unsupported marker

- `<vrf>.staticRoutes[]`
  - Input fields:
    - `prefix`
    - `nextHop.address`
    - `nextHop.interface`
  - VyOS IPv4:
    - `set vrf name '<name>' protocols static route '<prefix>' next-hop '<address>'`
    - `set vrf name '<name>' protocols static route '<prefix>' interface '<iface>'`
  - VyOS IPv6:
    - `set vrf name '<name>' protocols static route6 '<prefix>' next-hop '<address>'`

- `<vrf>.policyRoutes[]`
  - Input fields:
    - `trafficMatch.interface`
    - `trafficMatch.sourcePrefixes[]`
    - `trafficMatch.destinationPrefixes[]`
    - `trafficMatch.protocols[]`
    - `trafficMatch.sourcePorts[]`
    - `trafficMatch.destinationPorts[]`
    - `nextHop.address`
    - `nextHop.vrf`
  - VyOS route-policy root:
    - IPv4: `policy route`
    - IPv6: `policy route6`
  - Generated forms:
    - `set policy route '<name>' interface '<if>'`
    - `set policy route '<name>' rule '<id>' source address '<prefix>'`
    - `set policy route '<name>' rule '<id>' destination address '<prefix>'`
    - `set policy route '<name>' rule '<id>' protocol '<proto>'`
    - `set policy route '<name>' rule '<id>' source port '<port>'`
    - `set policy route '<name>' rule '<id>' destination port '<port>'`
    - `set policy route '<name>' rule '<id>' set nexthop '<address>'`
    - `set policy route '<name>' rule '<id>' set vrf '<vrf>'`
    - `set policy route '<name>' rule '<id>' set table '<table>'`
  - Priority: `nexthop.address` > `nexthop.vrf` > `vrf.table` > warning
  - Notes:
    - one VyOS rule per supported protocol (rule_id incremented)
    - address-family mismatch between nexthop and prefixes produces a warning

- `<vrf>.bgpPeers[]`
  - Supported companion fields:
    - `<vrf>.localASN` / `localAsn` / `systemASN` / `systemAs` / `asn`
    - `<vrf>.routerId`
  - Supported peer fields:
    - `address` / `peerAddress` / `neighborAddress` / `peerIP` / `neighborIP`
    - `remoteASN` / `remoteAsn` / `remoteAs` / `remote-as`
    - `updateSource` / `updateSrc`
    - `ebgpMultihop` / `ebgp-multihop` / `multihop`
    - `password` / `peerPassword` / `bgpPassword`
    - `keepalive` / `keepAlive` (or `timers.keepalive`)
    - `holdtime` / `holdTime` / `hold-time` (or `timers.holdtime`)
    - `bfd`
    - `gracefulRestart` / `graceful-restart`
    - `addressFamilies[]`
    - `ipv4.importFilter` / `ipv4.exportFilter`
    - `ipv6.importFilter` / `ipv6.exportFilter`
  - VyOS peer commands:
    - `set vrf name '<vrf>' protocols bgp system-as '<asn>'`
    - `set vrf name '<vrf>' protocols bgp parameters router-id '<id>'`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' remote-as '<asn>'`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' update-source '<value>'`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' ebgp-multihop '<n>'`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' password '<value>'`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' timers keepalive '<n>'`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' timers holdtime '<n>'`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' bfd`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' graceful-restart`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' address-family ipv4-unicast`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' address-family ipv6-unicast`
  - Notes:
    - keepalive and holdtime must both be present (VyOS requirement)
    - if `addressFamilies` is missing, the adapter infers from the peer IP family and warns
    - `ebgpMultihop` of 0 is silently skipped
    - unsupported peer fields remain surfaced explicitly

### BGP Import/Export Filters

Structured `importFilter` / `exportFilter` objects from the per-address-family
`ipv4` / `ipv6` objects are compiled into VyOS policy objects.

Filter structure:

- `defaultAction.type` — `accept` / `reject` (mapped to `permit` / `deny`)
- `items[]` — list of filter items, each with:
  - `action.type` — `accept` / `reject`
  - `action.modifyRoute.addCommunities[]` — community values to add
  - `action.modifyRoute.additiveCommunities` — append instead of replace
  - `action.modifyRoute.removeAllCommunities` — set community to `none`
  - `action.modifyRoute.removeCommunities[]` — specific community deletion
  - `matcher.prefix.prefix` — prefix to match
  - `matcher.prefix.ge` / `matcher.prefix.le` — length constraints
  - `matcher.bgpCommunity.community` — community regex
  - `matcher.bgpCommunity.exactMatch` — exact match flag

VyOS output:

- Route-map: `set policy route-map '<name>' rule '<N>' action 'permit|deny'`
- Prefix-list (IPv4): `set policy prefix-list '<name>' rule '10' prefix '<prefix>'`
- Prefix-list (IPv6): `set policy prefix-list6 '<name>' rule '10' prefix '<prefix>'`
- Community-list: `set policy community-list '<name>' rule '10' regex '<community>'`
- Binding: `set ... neighbor '<addr>' address-family <af> route-map import|export '<name>'`

Naming convention: `hbr-<vrf>-<sanitized-peer>-<af>-<direction>`

Notes:

- Simple string references (`routeMap`, `prefixList`, `distributionList`) remain unsupported
- Empty prefix or community strings are silently skipped (not emitted)
- Default action `"next"` is mapped to `deny`

## `NodeNetplanConfig`

### Interface Blocks

Supported sources:

- `spec.interfaces.*` / `spec.ethernets.*` (legacy format)
- `spec.desiredState.network.{ethernets,bonds,bridges,vlans,dummies,tunnels,wifis,modems}` (netplan native)
- `spec.desiredState.{ethernets,...}` (unwrapped variant)

For each interface:

- `<interface>.addresses[]`
  - VyOS:
    - `set interfaces <family> <name> address '<cidr>'`
  - Supported inferred families: same as VRF interface list above
  - Notes:
    - `ethernet` fallback for unrecognised prefixes

- `<interface>.dhcp4` / `<interface>.dhcp6`
  - VyOS:
    - `set interfaces <family> <name> address 'dhcp'`
    - `set interfaces <family> <name> address 'dhcp6'`

- `<interface>.mtu`
  - VyOS:
    - `set interfaces <family> <name> mtu '<value>'`

- `<interface>.routes[]`
  - Input fields:
    - `to`
    - `via`
    - `metric`
  - VyOS:
    - `set protocols static route '<to>' next-hop '<via>'`
    - `set protocols static route '<to>' next-hop '<via>' distance '<metric>'`
  - Notes:
    - IPv4/IPv6 auto-detected from prefix
    - missing `to` or `via` produces a warning and is skipped

### DNS / Name Servers

Supported sources:

- `spec.nameservers[]` / `spec.nameServers[]` / `spec.dns.addresses[]` (legacy)
- `spec.desiredState.network.nameservers.addresses[]` (netplan native)
- `spec.desiredState.nameservers[]` (unwrapped plain list)

VyOS:

- `set system name-server '<address>'`

## Controller Features

### Reconcile Layer

- Digest-based change detection (SHA-256 of command list)
- Diff-based partial delete on document modification
- Full teardown on document removal (coarse-grained for VRF/policy/BGP, fine-grained for netplan)
- Rollback on apply failure: `discard_pending()` clears staged VyOS state

### Status Contract

Per-document conditions patched to Kubernetes status subresource:

- `DesiredSeen` — desired revision and digest recorded
- `Applied` — applied revision and digest recorded
- `InSync` — desired and applied match
- `Reconciling` — pending apply or drift detected
- `Degraded` — last operation failed (carries error message)
- `Available` — adapter in sync with desired state
- `HasWarnings` — translation warnings present
- `HasUnsupported` — unsupported semantics present
- `Deleted` — document removed from source
- `Error` — last operation error

### Leader Election

- Kubernetes Lease-based (`coordination.k8s.io/v1`)
- Non-leaders reconcile locally but skip VyOS apply
- CLI: `--enable-leader-election`, `--leader-id`, `--lease-namespace`

### Informer Loop

- Background watch thread with event queue
- Immediate wake on change (no full-timeout blocking)
- Exponential backoff on errors (0.2s → 30s)
- Periodic full resync (30min default)

## Layer2 / VXLAN / EVPN

### Layer2 Domains (`spec.layer2s.<name>`)

Each layer2 entry creates a VXLAN interface and a bridge domain:

- `vni` — `set interfaces vxlan vxlan<VNI> vni '<vni>'` **[required]**
- `mtu` — `set interfaces vxlan vxlan<VNI> mtu '<mtu>'` **[required]**
- `vlan` — bridge name derived as `br<VLAN>` **[required]**
- `routeTarget` — L2 route target (not yet bound to VyOS L2 RT config)
- VXLAN parameters: `set interfaces vxlan vxlan<VNI> parameters nolearning` (implicit)
- Bridge-VXLAN binding: `set interfaces bridge br<VLAN> member interface vxlan<VNI>`

IRB (Integrated Routing and Bridging) — when `irb` is present:

- `irb.ipAddresses[]` — `set interfaces bridge br<VLAN> address '<ip/prefix>'`
- `irb.macAddress` — `set interfaces bridge br<VLAN> mac '<mac>'`
- `irb.vrf` — `set interfaces bridge br<VLAN> vrf '<vrf>'`

Mirror ACLs: detected and surfaced as unsupported (GRE mirroring is VyOS-version-dependent).

### Fabric VRF EVPN (`spec.fabricVRFs.<name>`)

When a fabric VRF has EVPN fields (vni, route targets, or export filter):

- `vni` — `set vrf name '<vrf>' vni '<vni>'`
- l2vpn-evpn activation: `set vrf name '<vrf>' protocols bgp address-family l2vpn-evpn`
- advertise-all-vni: emitted implicitly when EVPN fields present
- `evpnExportRouteTargets[]` — `set ... l2vpn-evpn route-target export '<rt>'`
- `evpnImportRouteTargets[]` — `set ... l2vpn-evpn route-target import '<rt>'`
- `evpnExportFilter` — compiled into VyOS route-map (same engine as BGP peer filters),
  bound via `route-map export '<name>'`
- `vrfImports[].fromVrf` — `set ... l2vpn-evpn import vrf '<from>'`
- `vrfImports[].filter` — compiled into VyOS route-map for the import

### EVPN Limitations and Design Decisions

- **VTEP source-address**: not in the CRD (it's a per-node property, not per-document).
  VyOS uses the loopback or default route as VXLAN source. For explicit control,
  use `--vtep-source-address` CLI parameter (future) or configure the loopback in
  the fabric VRF.
- **Route Distinguisher**: absent from CRD. VyOS auto-generates (`<router-id>:<vni>`).
- **advertise-all-vni**: no CRD flag — emitted implicitly when EVPN route targets are present.
- **Bridge member physical interfaces**: not in CRD. Physical interfaces are attached
  via the VRF `interfaces[]` field; bridge membership is limited to the VXLAN interface.
- **mirrorAcls**: surfaced as unsupported (GRE encapsulation for traffic mirroring is
  VyOS-version-dependent and not yet mapped).
- **l2vpn-evpn address family**: recognized in `addressFamilies` for BGP peers (alongside
  `ipv4-unicast` and `ipv6-unicast`).

## Unsupported Areas

The following are not mapped yet:

- Simple string route-map references (`routeMap`, `prefixList`, `distributionList`)
- `mirrorAcls` GRE traffic mirroring
- Full CRA rollout orchestration
- Per-interface readiness
- Exact host-netplan parity

## API Versioning and Auto-Discovery

The upstream Sylva network-connector project uses different API groups depending
on the deployment context. The adapter handles this transparently:

### Known API Groups

| API Group | Version | Origin |
|-----------|---------|--------|
| `network.t-caas.telekom.com` | `v1alpha1` | T-CAAS / Deutsche Telekom production |
| `network.t-caas.telekom.com` | `v1beta1` | Forward-compatible alias |
| `sylva.io` | `v1alpha1` | Upstream Sylva Linux Foundation project |

### Auto-Discovery Mechanism

At startup in `--source kubernetes` mode, the adapter registers all known API
variants and attempts to list documents from each. API groups that return 404
(not installed on the cluster) are silently skipped. No configuration flag is
needed.

This means:
- a cluster running `network.t-caas.telekom.com/v1alpha1` CRDs works out of the box
- a cluster running `sylva.io/v1alpha1` CRDs also works out of the box
- a cluster running both simultaneously discovers both (documents from all groups
  are merged into the same reconcile state)

### Adding New API Variants

New API groups can be registered at runtime before the first list/watch call:

```python
from hbr_vyos_adapter.k8s_resources import register_resource, CustomResourceSpec

register_resource(CustomResourceSpec(
    api_version="cloud.example.com/v1",
    kind="NodeNetworkConfig",
    plural="nodenetworkconfigs",
))
```

### Limitations

- The adapter does not call the Kubernetes API discovery endpoint
  (`/apis`). It relies on a hardcoded list of known variants. New upstream
  API groups require a code change (one line in `KNOWN_API_VARIANTS`).
- CRD schema validation is not performed by the adapter. The adapter parses
  what it understands and surfaces the rest as warnings or unsupported markers.
- The local CRD files in `k8s/crds/` are pinned to the `network.t-caas.telekom.com`
  API group. The drift check script (`scripts/check-sylva-drift.py`) compares
  against the upstream `sylva-elements/network-connector@main` repository.

## Mapping Principles

- prefer explicit command generation over hidden inference
- preserve unsupported items as warnings instead of silently dropping them
- only emit commands we can justify from the current VyOS command tree
