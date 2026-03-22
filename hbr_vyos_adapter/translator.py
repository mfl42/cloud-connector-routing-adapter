from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_address
from ipaddress import ip_interface
from ipaddress import ip_network

from .models import BgpPeer
from .models import NodeNetplanConfig
from .models import NodeNetworkConfig
from .models import PolicyRoute
from .models import StaticRoute
from .models import VrfSpec


@dataclass(slots=True)
class TranslationResult:
    commands: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    def extend(self, other: "TranslationResult") -> None:
        self.commands.extend(other.commands)
        self.warnings.extend(other.warnings)
        self.unsupported.extend(other.unsupported)


class VyosTranslator:
    _vrf_interface_types: tuple[str, ...] = (
        "ethernet",
        "bonding",
        "bridge",
        "pppoe",
        "dummy",
        "virtual-ethernet",
        "wireguard",
        "vti",
        "vxlan",
    )

    def translate(self, document: NodeNetworkConfig | NodeNetplanConfig) -> TranslationResult:
        if isinstance(document, NodeNetworkConfig):
            return self.translate_node_network_config(document)
        if isinstance(document, NodeNetplanConfig):
            return self.translate_node_netplan_config(document)
        raise TypeError(f"unsupported document type: {type(document)!r}")

    def translate_node_network_config(self, document: NodeNetworkConfig) -> TranslationResult:
        result = TranslationResult()
        if document.revision:
            result.warnings.append(f"tracking HBR revision {document.revision}")

        for vrf in _iter_vrfs(document):
            result.extend(self._translate_vrf(vrf))

        if document.layer2s:
            result.unsupported.append(
                "layer2s present: CRA/L2/VNI semantics are not mapped in this scaffold yet"
            )

        return result

    def translate_node_netplan_config(self, document: NodeNetplanConfig) -> TranslationResult:
        result = TranslationResult()
        for iface in document.interfaces.values():
            for address in iface.addresses:
                if not _is_valid_interface_address(address):
                    result.warnings.append(
                        f"interface {iface.name} address {address!r} is invalid; skipping"
                    )
                    continue
                result.commands.append(_netplan_address_command(iface.name, address))

            for route in iface.routes:
                if not route.to or not route.via:
                    result.warnings.append(
                        f"interface {iface.name} route is missing 'to' or 'via'; skipping"
                    )
                    continue

                family = _validated_prefix_family(
                    route.to,
                    warnings=result.warnings,
                    context=f"interface {iface.name} route destination {route.to!r}",
                )
                if family is None:
                    continue
                if not _is_valid_ip_literal(route.via):
                    result.warnings.append(
                        f"interface {iface.name} route via {route.via!r} is invalid; skipping"
                    )
                    continue
                if route.to == "0.0.0.0/0":
                    result.commands.append(
                        f"set protocols static route 0.0.0.0/0 next-hop '{route.via}'"
                    )
                elif route.to == "::/0":
                    result.commands.append(
                        f"set protocols static route6 ::/0 next-hop '{route.via}'"
                    )
                elif family == 4:
                    result.commands.append(
                        f"set protocols static route '{route.to}' next-hop '{route.via}'"
                    )
                else:
                    result.commands.append(
                        f"set protocols static route6 '{route.to}' next-hop '{route.via}'"
                    )

        for nameserver in document.nameservers:
            if not _is_valid_ip_literal(nameserver):
                result.warnings.append(f"nameserver {nameserver!r} is invalid; skipping")
                continue
            result.commands.append(f"set system name-server '{nameserver}'")

        return result

    def _translate_vrf(self, vrf: VrfSpec) -> TranslationResult:
        result = TranslationResult()
        if not vrf.name:
            result.unsupported.append("vrf entry has an empty name; skipping")
            return result
        if vrf.table is not None:
            result.commands.append(f"set vrf name '{vrf.name}' table '{vrf.table}'")
        elif vrf.static_routes or vrf.bgp_peers or vrf.policy_routes:
            result.warnings.append(f"vrf {vrf.name} has no table; route programming may be incomplete")

        if vrf.interfaces:
            for interface in vrf.interfaces:
                result.extend(self._translate_vrf_interface(vrf.name, interface))

        for index, route in enumerate(vrf.static_routes, start=10):
            result.extend(self._translate_static_route(vrf, route, index))

        for index, policy_route in enumerate(vrf.policy_routes, start=10):
            result.extend(self._translate_policy_route(vrf, policy_route, index))

        if vrf.bgp_peers:
            result.extend(self._translate_bgp(vrf))

        return result

    def _translate_static_route(self, vrf: VrfSpec, route: StaticRoute, _: int) -> TranslationResult:
        result = TranslationResult()
        if not route.prefix:
            result.warnings.append(f"vrf {vrf.name} contains a static route without a prefix")
            return result
        if not route.next_hop.address:
            result.warnings.append(f"vrf {vrf.name} route {route.prefix} is missing next-hop address")
            return result
        if vrf.table is None:
            result.warnings.append(f"vrf {vrf.name} route {route.prefix} skipped because vrf table is unset")
            return result

        family = _validated_prefix_family(
            route.prefix,
            warnings=result.warnings,
            context=f"vrf {vrf.name} static route prefix {route.prefix!r}",
        )
        if family is None:
            return result
        next_hop_family = _validated_ip_family(
            route.next_hop.address,
            warnings=result.warnings,
            context=f"vrf {vrf.name} static route next-hop {route.next_hop.address!r}",
        )
        if next_hop_family is None:
            return result
        if next_hop_family != family:
            result.warnings.append(
                f"vrf {vrf.name} route {route.prefix} next-hop {route.next_hop.address} "
                "uses a different address family; skipping"
            )
            return result
        if family == 4:
            result.commands.append(
                f"set vrf name '{vrf.name}' protocols static route '{route.prefix}' "
                f"next-hop '{route.next_hop.address}'"
            )
        else:
            result.commands.append(
                f"set vrf name '{vrf.name}' protocols static route6 '{route.prefix}' "
                f"next-hop '{route.next_hop.address}'"
            )
        return result

    def _translate_policy_route(
        self, vrf: VrfSpec, policy_route: PolicyRoute, rule_id: int
    ) -> TranslationResult:
        result = TranslationResult()
        traffic = policy_route.traffic_match
        family = _policy_address_family(
            traffic.source_prefixes,
            traffic.destination_prefixes,
            warnings=result.warnings,
            context=f"policy route hbr-{vrf.name} rule {rule_id}",
        )
        if family is None:
            return result
        policy_name = f"hbr-{vrf.name}"
        policy_root = "policy route6" if family == 6 else "policy route"

        if traffic.protocols:
            protocol = _first_protocol(traffic.protocols)
            if not protocol:
                result.warnings.append(
                    f"policy route {policy_name} rule {rule_id} has no supported protocol; skipping rule"
                )
                return result
        else:
            protocol = None

        if traffic.interface:
            result.commands.append(
                f"set {policy_root} '{policy_name}' interface '{traffic.interface}'"
            )
        else:
            result.warnings.append(
                f"policy route {policy_name} has no interface binding; emit rules only"
            )

        for prefix in _filter_prefixes_for_family(
            traffic.source_prefixes,
            family,
            warnings=result.warnings,
            context=f"policy route {policy_name} rule {rule_id} source",
        ):
            result.commands.append(
                f"set {policy_root} '{policy_name}' rule '{rule_id}' source address '{prefix}'"
            )
        for prefix in _filter_prefixes_for_family(
            traffic.destination_prefixes,
            family,
            warnings=result.warnings,
            context=f"policy route {policy_name} rule {rule_id} destination",
        ):
            result.commands.append(
                f"set {policy_root} '{policy_name}' rule '{rule_id}' destination address '{prefix}'"
            )

        if protocol:
            result.commands.append(
                f"set {policy_root} '{policy_name}' rule '{rule_id}' protocol '{protocol}'"
            )
        if traffic.source_ports:
            result.commands.append(
                f"set {policy_root} '{policy_name}' rule '{rule_id}' source port '{traffic.source_ports[0]}'"
            )
        if traffic.destination_ports:
            result.commands.append(
                f"set {policy_root} '{policy_name}' rule '{rule_id}' destination port '{traffic.destination_ports[0]}'"
            )

        if policy_route.next_hop.vrf:
            result.commands.append(
                f"set {policy_root} '{policy_name}' rule '{rule_id}' set vrf '{policy_route.next_hop.vrf}'"
            )
        elif vrf.table is not None:
            result.commands.append(
                f"set {policy_root} '{policy_name}' rule '{rule_id}' set table '{vrf.table}'"
            )
        else:
            result.warnings.append(
                f"policy route {policy_name} rule {rule_id} cannot resolve target table or target VRF"
            )

        if policy_route.next_hop.address:
            result.warnings.append(
                f"policy route {policy_name} rule {rule_id} carries next-hop "
                f"{policy_route.next_hop.address}; this scaffold maps policy rules to tables/VRFs, "
                "not direct next-hop actions"
            )

        return result

    def _translate_vrf_interface(self, vrf_name: str, interface: str) -> TranslationResult:
        result = TranslationResult()

        if "." in interface:
            base, _, vlan_id = interface.partition(".")
            if not vlan_id.isdigit():
                result.unsupported.append(
                    f"vrf {vrf_name} interface {interface} has a non-numeric VLAN id"
                )
                return result
            base_type = _infer_interface_type(base)
            if base_type is None:
                result.unsupported.append(
                    f"vrf {vrf_name} interface {interface} base {base!r} uses an unknown interface family"
                )
                return result
            if base_type not in self._vrf_interface_types:
                result.unsupported.append(
                    f"vrf {vrf_name} interface {interface} base {base!r} is inferred as {base_type}, "
                    "which is not in the supported attachment list for this scaffold"
                )
                return result
            result.commands.append(
                f"set interfaces {base_type} {base} vif '{vlan_id}' vrf '{vrf_name}'"
            )
            return result

        interface_type = _infer_interface_type(interface)
        if interface_type is None:
            result.unsupported.append(
                f"vrf {vrf_name} interface {interface} uses an unknown interface family"
            )
            return result

        if interface_type not in self._vrf_interface_types:
            result.unsupported.append(
                f"vrf {vrf_name} interface {interface} is inferred as {interface_type}, "
                "which is not in the supported attachment list for this scaffold"
            )
            return result

        result.commands.append(
            f"set interfaces {interface_type} {interface} vrf '{vrf_name}'"
        )
        return result

    def _translate_bgp(self, vrf: VrfSpec) -> TranslationResult:
        result = TranslationResult()
        if not vrf.bgp_system_as:
            result.warnings.append(
                f"vrf {vrf.name} has bgpPeers but no local ASN/system-as; skipping BGP translation"
            )
            return result

        bgp_root = f"set vrf name '{vrf.name}' protocols bgp"
        result.commands.append(f"{bgp_root} system-as '{vrf.bgp_system_as}'")

        for peer in vrf.bgp_peers:
            result.extend(self._translate_bgp_peer(vrf.name, bgp_root, peer))

        return result

    def _translate_bgp_peer(
        self, vrf_name: str, bgp_root: str, peer: BgpPeer
    ) -> TranslationResult:
        result = TranslationResult()
        if not peer.address:
            result.warnings.append(
                f"vrf {vrf_name} has a BGP peer without an address; skipping"
            )
            return result
        if not peer.remote_as:
            result.warnings.append(
                f"vrf {vrf_name} BGP peer {peer.address} is missing remote-as; skipping"
            )
            return result

        peer_root = f"{bgp_root} neighbor '{peer.address}'"
        result.commands.append(f"{peer_root} remote-as '{peer.remote_as}'")

        if peer.update_source:
            result.commands.append(f"{peer_root} update-source '{peer.update_source}'")
        if peer.ebgp_multihop is not None and peer.ebgp_multihop > 0:
            result.commands.append(f"{peer_root} ebgp-multihop '{peer.ebgp_multihop}'")

        families, unknown_families = _normalized_bgp_address_families(peer.address_families)
        if unknown_families:
            result.unsupported.append(
                f"vrf {vrf_name} BGP peer {peer.address} requests unsupported address families: "
                + ", ".join(unknown_families)
            )
        if not families:
            inferred_family = _infer_bgp_address_family(peer.address)
            if inferred_family is None:
                result.warnings.append(
                    f"vrf {vrf_name} BGP peer {peer.address} has no usable address family; "
                    "skipping address-family activation"
                )
            else:
                families = [inferred_family]
                result.warnings.append(
                    f"vrf {vrf_name} BGP peer {peer.address} has no addressFamilies; "
                    f"defaulting to {inferred_family}"
                )

        for family in families:
            result.commands.append(f"{peer_root} address-family {family}")

        unsupported_keys = sorted(
            key
            for key in peer.raw
            if key
            not in {
                "address",
                "peerAddress",
                "peerIP",
                "peerIp",
                "neighborAddress",
                "neighborIP",
                "neighborIp",
                "remoteASN",
                "remoteAsn",
                "remoteAs",
                "remoteAS",
                "remote-as",
                "updateSource",
                "updateSrc",
                "update-source",
                "ebgpMultihop",
                "ebgp-multihop",
                "multihop",
                "addressFamilies",
                "addressFamily",
                "address_families",
                "families",
                "afiSafis",
            }
        )
        if unsupported_keys:
            result.unsupported.append(
                f"vrf {vrf_name} BGP peer {peer.address} carries unsupported fields: "
                + ", ".join(unsupported_keys)
            )

        return result


def _iter_vrfs(document: NodeNetworkConfig) -> list[VrfSpec]:
    vrfs: list[VrfSpec] = []
    if document.cluster_vrf is not None:
        vrfs.append(document.cluster_vrf)
    vrfs.extend(document.fabric_vrfs.values())
    vrfs.extend(document.local_vrfs.values())
    return vrfs


def _first_protocol(protocols: list[str]) -> str | None:
    supported = {"tcp", "udp", "tcp_udp", "icmp", "icmpv6"}
    for protocol in protocols:
        if protocol in supported:
            return protocol
    return None


def _address_family(prefix: str) -> int:
    return ip_network(prefix, strict=False).version


def _policy_address_family(
    source: list[str],
    destination: list[str],
    *,
    warnings: list[str],
    context: str,
) -> int | None:
    families: set[int] = set()
    all_prefixes = [*source, *destination]
    for prefix in all_prefixes:
        family = _validated_prefix_family(prefix, warnings=warnings, context=f"{context} prefix {prefix!r}")
        if family is not None:
            families.add(family)
    if not families:
        if all_prefixes:
            warnings.append(f"{context} has no valid prefixes; skipping rule")
            return None
        return 4
    if len(families) > 1:
        warnings.append(f"{context} mixes IPv4 and IPv6 prefixes; skipping rule")
        return None
    return next(iter(families))


def _infer_interface_type(interface: str) -> str | None:
    prefixes = (
        ("eth", "ethernet"),
        ("en", "ethernet"),
        ("bond", "bonding"),
        ("br", "bridge"),
        ("pppoe", "pppoe"),
        ("dum", "dummy"),
        ("veth", "virtual-ethernet"),
        ("wg", "wireguard"),
        ("vti", "vti"),
        ("vxlan", "vxlan"),
    )
    for prefix, interface_type in prefixes:
        if interface.startswith(prefix):
            return interface_type
    return None


def _normalized_bgp_address_families(families: list[str]) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for family in families:
        canonical = _normalize_bgp_address_family(family)
        if canonical is None:
            unknown.append(family)
            continue
        if canonical in seen:
            continue
        normalized.append(canonical)
        seen.add(canonical)
    return normalized, unknown


def _normalize_bgp_address_family(family: str) -> str | None:
    token = family.strip().lower().replace("_", "-")
    if token in {"ipv4", "ipv4-unicast", "v4", "inet"}:
        return "ipv4-unicast"
    if token in {"ipv6", "ipv6-unicast", "v6", "inet6"}:
        return "ipv6-unicast"
    return None


def _infer_bgp_address_family(peer_address: str) -> str | None:
    try:
        family = ip_address(peer_address).version
    except ValueError:
        return None
    return "ipv6-unicast" if family == 6 else "ipv4-unicast"


def _validated_prefix_family(prefix: str, *, warnings: list[str], context: str) -> int | None:
    try:
        return _address_family(prefix)
    except ValueError:
        warnings.append(f"{context} is invalid; skipping")
        return None


def _validated_ip_family(address: str, *, warnings: list[str], context: str) -> int | None:
    try:
        return ip_address(address).version
    except ValueError:
        warnings.append(f"{context} is invalid; skipping")
        return None


def _filter_prefixes_for_family(
    prefixes: list[str],
    family: int,
    *,
    warnings: list[str],
    context: str,
) -> list[str]:
    valid: list[str] = []
    for prefix in prefixes:
        prefix_family = _validated_prefix_family(
            prefix,
            warnings=warnings,
            context=f"{context} prefix {prefix!r}",
        )
        if prefix_family is None:
            continue
        if prefix_family != family:
            warnings.append(
                f"{context} prefix {prefix!r} does not match the rule address family; skipping"
            )
            continue
        valid.append(prefix)
    return valid


def _netplan_address_command(interface: str, address: str) -> str:
    """Generate a VyOS set command for an interface address, handling VLAN subinterfaces."""
    if "." in interface:
        base, _, vlan_id = interface.partition(".")
        if vlan_id.isdigit():
            base_type = _infer_interface_type(base) or "ethernet"
            return f"set interfaces {base_type} {base} vif '{vlan_id}' address '{address}'"
    return f"set interfaces ethernet {interface} address '{address}'"


def _is_valid_interface_address(value: str) -> bool:
    try:
        ip_interface(value)
    except ValueError:
        return False
    return True


def _is_valid_ip_literal(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True
