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

## Unsupported Areas

The following are not mapped yet:

- `layer2s` / EVPN / VNI / VXLAN constructs (deferred — upstream API unstable)
- Simple string route-map references (`routeMap`, `prefixList`, `distributionList`)
- Full CRA rollout orchestration
- Per-interface readiness
- Exact host-netplan parity

## Mapping Principles

- prefer explicit command generation over hidden inference
- preserve unsupported items as warnings instead of silently dropping them
- only emit commands we can justify from the current VyOS command tree
