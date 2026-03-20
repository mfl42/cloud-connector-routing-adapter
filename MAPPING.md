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
  - VyOS output:
    - no direct config command yet
  - Reason:
    - revision/status management belongs to the future reconcile layer

- `spec.layer2s`
  - Current behavior:
    - reported as unsupported
  - VyOS output:
    - none
  - Reason:
    - CRA/L2/VNI semantics need a more explicit design

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
    - unknown families remain unsupported

- `<vrf>.staticRoutes[]`
  - Input fields:
    - `prefix`
    - `nextHop.address`
  - VyOS IPv4:
    - `set vrf name '<name>' protocols static route '<prefix>' next-hop '<address>'`
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
    - `nextHop.vrf`
    - `nextHop.address`
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
    - `set policy route '<name>' rule '<id>' set vrf '<vrf>'`
    - or:
      - `set policy route '<name>' rule '<id>' set table '<table>'`
  - Notes:
    - direct next-hop action is not emitted yet
    - when `nextHop.address` exists it is currently surfaced as a warning

- `<vrf>.bgpPeers`
  - Supported companion fields:
    - `<vrf>.localASN`
    - `<vrf>.localAsn`
    - `<vrf>.systemASN`
    - `<vrf>.systemAs`
    - `<vrf>.asn`
  - Supported peer fields:
    - `address`
    - `peerAddress`
    - `neighborAddress`
    - `remoteASN`
    - `remoteAsn`
    - `remoteAs`
    - `updateSource`
    - `updateSrc`
    - `ebgpMultihop`
    - `addressFamilies[]`
  - VyOS:
    - `set vrf name '<vrf>' protocols bgp system-as '<asn>'`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' remote-as '<asn>'`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' update-source '<value>'`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' ebgp-multihop '<n>'`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' address-family ipv4-unicast`
    - `set vrf name '<vrf>' protocols bgp neighbor '<addr>' address-family ipv6-unicast`
  - Notes:
    - only a conservative unicast neighbor subset is emitted
    - if `addressFamilies` is missing, the adapter defaults from the peer IP family and warns
    - unsupported peer fields remain surfaced explicitly

## `NodeNetplanConfig`

### Interface Blocks

Supported sources:

- `spec.interfaces.*`
- `spec.ethernets.*`

For each interface:

- `<interface>.addresses[]`
  - VyOS:
    - `set interfaces ethernet <name> address '<cidr>'`
  - Notes:
    - current scaffold assumes ethernet here
    - more interface families can be added later

- `<interface>.routes[]`
  - Input fields:
    - `to`
    - `via`
  - VyOS default IPv4 route:
    - `set protocols static route 0.0.0.0/0 next-hop '<via>'`
  - VyOS default IPv6 route:
    - `set protocols static route6 ::/0 next-hop '<via>'`
  - VyOS IPv4 route:
    - `set protocols static route '<to>' next-hop '<via>'`
  - VyOS IPv6 route:
    - `set protocols static route6 '<to>' next-hop '<via>'`

### DNS / Name Servers

- `spec.nameservers[]`
- `spec.nameServers[]`
- `spec.dns.addresses[]`
  - VyOS:
    - `set system name-server '<address>'`

## Unsupported Areas

The following are not mapped yet:

- full CRA integration contract
- status/revision writeback
- rollout orchestration
- `layer2s` / EVPN / VNI constructs
- full BGP object mapping beyond basic neighbor synthesis
- exact host-netplan parity
- advanced interface types in `NodeNetplanConfig`

## Mapping Principles

- prefer explicit command generation over hidden inference
- preserve unsupported items as warnings instead of silently dropping them
- only emit commands we can justify from the current VyOS command tree
