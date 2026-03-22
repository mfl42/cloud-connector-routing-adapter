from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Metadata:
    name: str
    namespace: str | None = None
    generation: int | None = None
    resource_version: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Metadata":
        data = _mapping_or_raise(data, "metadata")
        return cls(
            name=data.get("name", "unnamed"),
            namespace=data.get("namespace"),
            generation=_int_or_none(data.get("generation")),
            resource_version=_string_or_none(data.get("resourceVersion")),
        )


@dataclass(slots=True)
class NextHop:
    address: str | None = None
    vrf: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NextHop":
        data = _mapping_or_raise(data, "nextHop")
        return cls(address=data.get("address"), vrf=data.get("vrf"))


@dataclass(slots=True)
class TrafficMatch:
    source_prefixes: list[str] = field(default_factory=list)
    destination_prefixes: list[str] = field(default_factory=list)
    source_ports: list[str] = field(default_factory=list)
    destination_ports: list[str] = field(default_factory=list)
    protocols: list[str] = field(default_factory=list)
    interface: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrafficMatch":
        data = _mapping_or_raise(data, "trafficMatch")
        source = _mapping_or_raise(data.get("source"), "trafficMatch.source", allow_none=True)
        destination = _mapping_or_raise(
            data.get("destination"), "trafficMatch.destination", allow_none=True
        )
        protocols = data.get("protocols") or data.get("protocol") or []

        if isinstance(protocols, str):
            protocols = [protocols]

        return cls(
            source_prefixes=_string_list(
                data.get("sourcePrefixes")
                or source.get("prefixes")
                or source.get("addresses")
                or source.get("address")
            ),
            destination_prefixes=_string_list(
                data.get("destinationPrefixes")
                or destination.get("prefixes")
                or destination.get("addresses")
                or destination.get("address")
            ),
            source_ports=_string_list(
                data.get("sourcePorts") or source.get("ports") or source.get("port")
            ),
            destination_ports=_string_list(
                data.get("destinationPorts")
                or destination.get("ports")
                or destination.get("port")
            ),
            # Normalise to lowercase with underscores. translator._SUPPORTED_PROTOCOLS
            # must match this canonical form (e.g. "tcp-udp" → "tcp_udp").
            protocols=[str(item).lower().replace("-", "_") for item in protocols],
            interface=data.get("interface") or data.get("inboundInterface"),
        )


@dataclass(slots=True)
class PolicyRoute:
    traffic_match: TrafficMatch
    next_hop: NextHop

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyRoute":
        data = _mapping_or_raise(data, "policyRoute")
        return cls(
            traffic_match=TrafficMatch.from_dict(data.get("trafficMatch", {})),
            next_hop=NextHop.from_dict(data.get("nextHop", {})),
        )


@dataclass(slots=True)
class StaticRoute:
    prefix: str
    next_hop: NextHop

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StaticRoute":
        data = _mapping_or_raise(data, "staticRoute")
        return cls(
            prefix=data.get("prefix") or data.get("destination") or "",
            next_hop=NextHop.from_dict(data.get("nextHop", {})),
        )


@dataclass(slots=True)
class BgpPeer:
    address: str | None = None
    remote_as: str | None = None
    update_source: str | None = None
    ebgp_multihop: int | None = None
    password: str | None = None
    keepalive: int | None = None
    holdtime: int | None = None
    bfd: bool = False
    graceful_restart: bool = False
    address_families: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BgpPeer":
        data = _mapping_or_raise(data, "bgpPeer")
        timers = _mapping_or_raise(data.get("timers"), "bgpPeer.timers", allow_none=True)
        return cls(
            address=_string_or_none(
                _first_value(
                    data,
                    "address",
                    "peerAddress",
                    "peerIP",
                    "peerIp",
                    "neighborAddress",
                    "neighborIP",
                    "neighborIp",
                )
            ),
            remote_as=_string_or_none(
                _first_value(
                    data,
                    "remoteASN",
                    "remoteAsn",
                    "remoteAs",
                    "remoteAS",
                    "remote-as",
                )
            ),
            update_source=_string_or_none(
                _first_value(data, "updateSource", "updateSrc", "update-source")
            ),
            ebgp_multihop=_int_or_none(
                _first_value(data, "ebgpMultihop", "ebgp-multihop", "multihop")
            ),
            password=_string_or_none(
                _first_value(data, "password", "peerPassword", "bgpPassword")
            ),
            keepalive=_int_or_none(
                _first_value(data, "keepalive", "keepAlive")
                or timers.get("keepalive")
                or timers.get("keepAlive")
            ),
            holdtime=_int_or_none(
                _first_value(data, "holdtime", "holdTime", "hold-time")
                or timers.get("holdtime")
                or timers.get("holdTime")
                or timers.get("hold-time")
            ),
            bfd=bool(data.get("bfd", False)),
            graceful_restart=bool(
                _first_value(data, "gracefulRestart", "graceful-restart") or False
            ),
            address_families=_string_list(
                _first_value(
                    data,
                    "addressFamilies",
                    "addressFamily",
                    "address_families",
                    "families",
                    "afiSafis",
                )
            ),
            raw=data,
        )


@dataclass(slots=True)
class VrfSpec:
    name: str
    table: int | None = None
    bgp_system_as: str | None = None
    bgp_router_id: str | None = None
    bgp_peers: list[BgpPeer] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)
    policy_routes: list[PolicyRoute] = field(default_factory=list)
    static_routes: list[StaticRoute] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "VrfSpec":
        if not name or not name.strip():
            raise ValueError("VRF name must be a non-empty string")
        data = _mapping_or_raise(data, f"vrf {name}")
        bgp_data = _mapping_or_raise(data.get("bgp"), f"vrf {name}.bgp", allow_none=True)
        bgp_system_as = _string_or_none(
            _first_value(
                data,
                "localASN",
                "localAsn",
                "localAs",
                "systemASN",
                "systemAs",
                "asn",
            )
            or _first_value(
                bgp_data,
                "localASN",
                "localAsn",
                "localAs",
                "systemASN",
                "systemAs",
                "asn",
            )
        )
        bgp_peers_raw = data.get("bgpPeers")
        if bgp_peers_raw is None:
            bgp_peers_raw = _first_value(bgp_data, "peers", "neighbors")

        bgp_router_id = _string_or_none(
            _first_value(data, "routerId", "router-id", "bgpRouterId")
            or _first_value(bgp_data, "routerId", "router-id", "routerID")
        )

        return cls(
            name=name,
            table=_int_or_none(data.get("table")),
            bgp_system_as=bgp_system_as,
            bgp_router_id=bgp_router_id,
            bgp_peers=_bgp_peers_from_raw(bgp_peers_raw),
            interfaces=_string_list(data.get("interfaces")),
            policy_routes=[
                PolicyRoute.from_dict(item)
                for item in _list_or_raise(data.get("policyRoutes"), f"vrf {name}.policyRoutes")
            ],
            static_routes=[
                StaticRoute.from_dict(item)
                for item in _list_or_raise(data.get("staticRoutes"), f"vrf {name}.staticRoutes")
            ],
            raw=data,
        )


@dataclass(slots=True)
class NodeNetworkConfig:
    api_version: str
    kind: str
    metadata: Metadata
    revision: str
    cluster_vrf: VrfSpec | None
    fabric_vrfs: dict[str, VrfSpec]
    local_vrfs: dict[str, VrfSpec]
    layer2s: dict[str, Any]
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeNetworkConfig":
        data = _mapping_or_raise(data, "NodeNetworkConfig")
        spec = _mapping_or_raise(data.get("spec"), "NodeNetworkConfig.spec", allow_none=True)
        cluster_vrf_data = spec.get("clusterVRF")
        cluster_vrf = None
        if cluster_vrf_data is not None:
            cluster_vrf_data = _mapping_or_raise(
                cluster_vrf_data, "NodeNetworkConfig.spec.clusterVRF"
            )
            cluster_name = cluster_vrf_data.get("name", "cluster")
            cluster_vrf = VrfSpec.from_dict(cluster_name, cluster_vrf_data)

        fabric_vrfs = {
            name: VrfSpec.from_dict(name, value)
            for name, value in _mapping_or_raise(
                spec.get("fabricVRFs"), "NodeNetworkConfig.spec.fabricVRFs", allow_none=True
            ).items()
            if isinstance(value, dict)
        }
        local_vrfs = {
            name: VrfSpec.from_dict(name, value)
            for name, value in _mapping_or_raise(
                spec.get("localVRFs"), "NodeNetworkConfig.spec.localVRFs", allow_none=True
            ).items()
            if isinstance(value, dict)
        }

        return cls(
            api_version=data.get("apiVersion", ""),
            kind=data.get("kind", ""),
            metadata=Metadata.from_dict(data.get("metadata", {})),
            revision=str(spec.get("revision", "")),
            cluster_vrf=cluster_vrf,
            fabric_vrfs=fabric_vrfs,
            local_vrfs=local_vrfs,
            layer2s=spec.get("layer2s") or {},
            raw=data,
        )


@dataclass(slots=True)
class RouteConfig:
    to: str
    via: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RouteConfig":
        data = _mapping_or_raise(data, "route")
        return cls(to=data.get("to", ""), via=data.get("via"))


@dataclass(slots=True)
class InterfaceConfig:
    name: str
    addresses: list[str] = field(default_factory=list)
    routes: list[RouteConfig] = field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "InterfaceConfig":
        data = _mapping_or_raise(data, f"interface {name}")
        return cls(
            name=name,
            addresses=_string_list(data.get("addresses") or data.get("address")),
            routes=[
                RouteConfig.from_dict(item)
                for item in _list_or_raise(data.get("routes"), f"interface {name}.routes")
            ],
        )


@dataclass(slots=True)
class NodeNetplanConfig:
    api_version: str
    kind: str
    metadata: Metadata
    interfaces: dict[str, InterfaceConfig]
    nameservers: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeNetplanConfig":
        data = _mapping_or_raise(data, "NodeNetplanConfig")
        spec = _mapping_or_raise(data.get("spec"), "NodeNetplanConfig.spec", allow_none=True)
        interfaces: dict[str, InterfaceConfig] = {}
        for section in ("interfaces", "ethernets"):
            for name, value in _mapping_or_raise(
                spec.get(section), f"NodeNetplanConfig.spec.{section}", allow_none=True
            ).items():
                if isinstance(value, dict):
                    interfaces[name] = InterfaceConfig.from_dict(name, value)

        nameservers = _string_list(
            spec.get("nameservers")
            or spec.get("nameServers")
            or (spec.get("dns") or {}).get("addresses")
        )

        return cls(
            api_version=data.get("apiVersion", ""),
            kind=data.get("kind", ""),
            metadata=Metadata.from_dict(data.get("metadata", {})),
            interfaces=interfaces,
            nameservers=nameservers,
            raw=data,
        )


def load_document(data: dict[str, Any]) -> NodeNetworkConfig | NodeNetplanConfig:
    kind = data.get("kind")
    if kind == "NodeNetworkConfig":
        return NodeNetworkConfig.from_dict(data)
    if kind == "NodeNetplanConfig":
        return NodeNetplanConfig.from_dict(data)
    raise ValueError(f"unsupported document kind: {kind!r}")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _string_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _bgp_peers_from_raw(value: Any) -> list[BgpPeer]:
    if value is None:
        return []

    peers: list[BgpPeer] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                peers.append(BgpPeer.from_dict(item))
        return peers

    if isinstance(value, dict):
        for address, item in value.items():
            if not isinstance(item, dict):
                continue
            peer_data = dict(item)
            peer_data.setdefault("address", address)
            peers.append(BgpPeer.from_dict(peer_data))

    return peers


def _mapping_or_raise(value: Any, context: str, *, allow_none: bool = False) -> dict[str, Any]:
    if value is None and allow_none:
        return {}
    if isinstance(value, dict):
        return value
    raise ValueError(f"{context} must be an object")


def _list_or_raise(value: Any, context: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    raise ValueError(f"{context} must be a list")
