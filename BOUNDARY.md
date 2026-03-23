# Boundary Testing

This runbook covers boundary-value and edge-case testing for the adapter.

Run the local boundary harness with:

```bash
python3 scripts/boundary-hbr-api-local.py
```

## Scenarios (17)

1. **interface-boundaries** — supported Ethernet, unknown family rejection,
   VLAN sub-interface handling
2. **static-and-policy-boundaries** — IPv4/IPv6 prefixes, missing fields,
   policy rules with nexthop/vrf/table, port extremes
3. **bgp-boundaries** — address-family inference, duplicate normalization,
   peers without address/remote-as, unsupported fields, solo holdtime warning
4. **netplan-boundaries** — IPv6 default route, hostish prefixes, missing
   route via
5. **invalid-value-boundaries** — malformed prefixes, invalid IPs,
   address-family mismatch, mixed IPv4/IPv6, invalid nameservers, unsupported
   protocols
6. **zero-command-reconcile-boundary** — empty document converges without API
   call, state file written, digest stable
7. **malformed-structure-boundaries** — spec not object, nextHop not object,
   routes not list
8. **desired-state-boundaries** — netplan desiredState wrapped/unwrapped,
   bonds, empty
9. **bgp-delete-regex-quotes** — BGP neighbor regex with plain and
   escaped-quote VRF/neighbor names
10. **large-topology** — 10 VRFs x 10 peers x 2 VLANs x 100 routes = 1240
    commands, no duplicates, second reconcile = noop
11. **cluster-scoped-url-routing** — _resource_url() for namespace-scoped,
    cluster-scoped, namespace=None, non-default namespace
12. **cra-status-conditions** — Reconciling/Degraded/Available for all 5
    phase states (InSync, PendingApply, Drifted, Error, Deleted)
13. **bgp-route-map-compilation** — import filter (prefix+community+
    exact-match+community modifications), export filter (default only),
    IPv6 import (prefix-list6), peer without filter, binding
14. **leader-election-boundaries** — NoopLeaseManager always-leader,
    LeaseState expiry (fresh/old/None), _parse_lease round-trip
15. **bgp-filter-edge-cases** — action "next" default, action-only item,
    empty prefix/community, ge>le, conflicting communities, export-only
16. **evpn-vxlan-layer2** — fabric VRF EVPN (vni, route targets,
    advertise-all-vni, export filter, VRF import), Layer2 VXLAN + bridge +
    IRB, l2vpn-evpn address family
17. **api-autodiscovery** — activate_all_known_variants registration,
    idempotent, _get_json_safe 404 tolerance, URL construction t-caas vs
    sylva.io API groups

## Artifacts

```text
artifacts/private/hbr-boundary-local/
```
