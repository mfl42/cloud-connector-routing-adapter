#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = REPO_ROOT / "artifacts" / "private" / "hbr-boundary-local"

import sys

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hbr_vyos_adapter.k8s_documents import KubeDocumentClient
from hbr_vyos_adapter.k8s_resources import CustomResourceSpec
from hbr_vyos_adapter.k8s_status import KubeConnection
from hbr_vyos_adapter.models import load_document
from hbr_vyos_adapter.reconcile import reconcile_documents, _BGP_NEIGHBOR_RE, _SCALAR_LEAF_RE
from hbr_vyos_adapter.state import DocumentState, ReconcileState
from hbr_vyos_adapter.status import build_status_report
from hbr_vyos_adapter.translator import VyosTranslator


def reset_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload) -> None:
    if hasattr(payload, "to_dict"):
        payload = payload.to_dict()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def scenario_interface_boundaries() -> dict:
    scenario_dir = ARTIFACT_DIR / "interface-boundaries"
    reset_dir(scenario_dir)
    document = load_document(
        {
            "apiVersion": "network.t-caas.telekom.com/v1alpha1",
            "kind": "NodeNetworkConfig",
            "metadata": {"name": "boundary-interface-node"},
            "spec": {
                "revision": "boundary-interfaces-1",
                "localVRFs": {
                    "edge-a": {
                        "table": 1,
                        "interfaces": ["eth1", "lo", "myst0"],
                    }
                },
            },
        }
    )

    result = VyosTranslator().translate(document)
    write_json(
        scenario_dir / "translation.json",
        {
            "commands": result.commands,
            "warnings": result.warnings,
            "unsupported": result.unsupported,
        },
    )

    assert "set vrf name 'edge-a' table '1'" in result.commands, result.commands
    assert "set interfaces ethernet eth1 vrf 'edge-a'" in result.commands, result.commands
    assert any("tracking HBR revision boundary-interfaces-1" == warning for warning in result.warnings)
    # `lo` is no longer a named type in the translator; both lo and myst0 fall
    # through to the generic "unknown interface family" unsupported message.
    assert any("interface lo" in item for item in result.unsupported), result.unsupported
    assert any("unknown interface family" in item for item in result.unsupported), result.unsupported

    return {
        "scenario": "interface-boundaries",
        "commandCount": len(result.commands),
        "warningCount": len(result.warnings),
        "unsupportedCount": len(result.unsupported),
    }


def scenario_static_and_policy_boundaries() -> dict:
    scenario_dir = ARTIFACT_DIR / "static-and-policy-boundaries"
    reset_dir(scenario_dir)
    document = load_document(
        {
            "apiVersion": "network.t-caas.telekom.com/v1alpha1",
            "kind": "NodeNetworkConfig",
            "metadata": {"name": "boundary-route-node"},
            "spec": {
                "revision": "boundary-routes-1",
                "localVRFs": {
                    "table-low": {
                        "table": 1,
                        "staticRoutes": [
                            {
                                "prefix": "198.51.100.7/32",
                                "nextHop": {"address": "192.0.2.1"},
                            },
                            {
                                "prefix": "2001:db8:1::7/128",
                                "nextHop": {"address": "2001:db8::1"},
                            },
                        ],
                    },
                    "policy-edge": {
                        "policyRoutes": [
                            {
                                "trafficMatch": {
                                    "sourcePrefixes": ["10.0.0.1/32"],
                                    "destinationPrefixes": ["172.16.0.1/32"],
                                    "protocols": ["gre", "udp"],
                                    "sourcePorts": ["1"],
                                    "destinationPorts": ["65535"],
                                },
                                "nextHop": {"address": "192.0.2.9"},
                            }
                        ],
                        "staticRoutes": [
                            {
                                "prefix": "203.0.113.0/24",
                                "nextHop": {},
                            },
                            {
                                "prefix": "",
                                "nextHop": {"address": "192.0.2.5"},
                            },
                        ],
                    },
                },
            },
        }
    )

    result = VyosTranslator().translate(document)
    write_json(
        scenario_dir / "translation.json",
        {
            "commands": result.commands,
            "warnings": result.warnings,
            "unsupported": result.unsupported,
        },
    )

    assert (
        "set vrf name 'table-low' protocols static route '198.51.100.7/32' next-hop '192.0.2.1'"
        in result.commands
    ), result.commands
    assert (
        "set vrf name 'table-low' protocols static route6 '2001:db8:1::7/128' next-hop '2001:db8::1'"
        in result.commands
    ), result.commands
    assert (
        "set policy route 'hbr-policy-edge' rule '10' protocol 'udp'" in result.commands
    ), result.commands
    assert not any("set table" in command and "hbr-policy-edge" in command for command in result.commands)
    assert any("set policy route 'hbr-policy-edge' rule '10' set nexthop '192.0.2.9'" in command for command in result.commands), result.commands
    assert any("has no interface binding" in warning for warning in result.warnings), result.warnings
    assert any("missing next-hop address" in warning for warning in result.warnings), result.warnings
    assert any("without a prefix" in warning for warning in result.warnings), result.warnings

    return {
        "scenario": "static-and-policy-boundaries",
        "commandCount": len(result.commands),
        "warningCount": len(result.warnings),
        "unsupportedCount": len(result.unsupported),
    }


def scenario_bgp_boundaries() -> dict:
    scenario_dir = ARTIFACT_DIR / "bgp-boundaries"
    reset_dir(scenario_dir)
    document = load_document(
        {
            "apiVersion": "network.t-caas.telekom.com/v1alpha1",
            "kind": "NodeNetworkConfig",
            "metadata": {"name": "boundary-bgp-node"},
            "spec": {
                "revision": "boundary-bgp-1",
                "localVRFs": {
                    "bgp-edge": {
                        "table": 4096,
                        "localASN": 65050,
                        "bgpPeers": [
                            {
                                "address": "2001:db8::1",
                                "remoteASN": 65051,
                            },
                            {
                                "address": "192.0.2.5",
                                "remoteASN": 65052,
                                "addressFamilies": [
                                    "ipv4",
                                    "ipv4-unicast",
                                    "bogus-af",
                                ],
                                "holdTime": 30,
                            },
                            {
                                "address": "192.0.2.6",
                            },
                            {
                                "remoteASN": 65054,
                            },
                        ],
                    }
                },
            },
        }
    )

    result = VyosTranslator().translate(document)
    write_json(
        scenario_dir / "translation.json",
        {
            "commands": result.commands,
            "warnings": result.warnings,
            "unsupported": result.unsupported,
        },
    )

    assert "set vrf name 'bgp-edge' protocols bgp system-as '65050'" in result.commands
    assert (
        "set vrf name 'bgp-edge' protocols bgp neighbor '2001:db8::1' address-family ipv6-unicast"
        in result.commands
    ), result.commands
    assert (
        "set vrf name 'bgp-edge' protocols bgp neighbor '192.0.2.5' address-family ipv4-unicast"
        in result.commands
    ), result.commands
    assert sum("192.0.2.5' address-family ipv4-unicast" in command for command in result.commands) == 1
    assert any("defaulting to ipv6-unicast" in warning for warning in result.warnings), result.warnings
    assert any("missing remote-as" in warning for warning in result.warnings), result.warnings
    assert any("without an address" in warning for warning in result.warnings), result.warnings
    assert any("unsupported address families: bogus-af" in item for item in result.unsupported), result.unsupported
    # holdTime is now a recognised timer field; a solo holdtime (no keepalive)
    # produces a warning rather than an unsupported entry.
    assert any("only one of keepalive/holdtime" in warning for warning in result.warnings), result.warnings

    return {
        "scenario": "bgp-boundaries",
        "commandCount": len(result.commands),
        "warningCount": len(result.warnings),
        "unsupportedCount": len(result.unsupported),
    }


def scenario_netplan_boundaries() -> dict:
    scenario_dir = ARTIFACT_DIR / "netplan-boundaries"
    reset_dir(scenario_dir)
    document = load_document(
        {
            "apiVersion": "network.t-caas.telekom.com/v1alpha1",
            "kind": "NodeNetplanConfig",
            "metadata": {"name": "boundary-netplan-node"},
            "spec": {
                "interfaces": {
                    "eth9": {
                        "addresses": ["192.0.2.10/24"],
                        "routes": [
                            {"to": "::/0", "via": "2001:db8::1"},
                            {"to": "2001:db8:2::7/64", "via": "2001:db8::2"},
                            {"to": "0.0.0.0/0"},
                        ],
                    }
                },
                "nameservers": ["2001:4860:4860::8888", "9.9.9.9"],
            },
        }
    )

    result = VyosTranslator().translate(document)
    write_json(
        scenario_dir / "translation.json",
        {
            "commands": result.commands,
            "warnings": result.warnings,
            "unsupported": result.unsupported,
        },
    )

    assert "set interfaces ethernet eth9 address '192.0.2.10/24'" in result.commands
    assert "set protocols static route6 ::/0 next-hop '2001:db8::1'" in result.commands
    assert (
        "set protocols static route6 '2001:db8:2::7/64' next-hop '2001:db8::2'" in result.commands
    )
    assert "set system name-server '2001:4860:4860::8888'" in result.commands
    assert "set system name-server '9.9.9.9'" in result.commands
    assert any("missing 'to' or 'via'; skipping" in warning for warning in result.warnings), result.warnings

    return {
        "scenario": "netplan-boundaries",
        "commandCount": len(result.commands),
        "warningCount": len(result.warnings),
        "unsupportedCount": len(result.unsupported),
    }


def scenario_invalid_value_boundaries() -> dict:
    scenario_dir = ARTIFACT_DIR / "invalid-value-boundaries"
    reset_dir(scenario_dir)

    network_document = load_document(
        {
            "apiVersion": "network.t-caas.telekom.com/v1alpha1",
            "kind": "NodeNetworkConfig",
            "metadata": {"name": "boundary-invalid-network"},
            "spec": {
                "revision": "boundary-invalid-network-1",
                "localVRFs": {
                    "invalid-a": {
                        "table": 2000,
                        "staticRoutes": [
                            {
                                "prefix": "not-a-prefix",
                                "nextHop": {"address": "192.0.2.1"},
                            },
                            {
                                "prefix": "198.51.100.0/24",
                                "nextHop": {"address": "not-an-ip"},
                            },
                            {
                                "prefix": "2001:db8:1::/64",
                                "nextHop": {"address": "192.0.2.9"},
                            },
                        ],
                        "policyRoutes": [
                            {
                                "trafficMatch": {
                                    "sourcePrefixes": ["not-a-prefix"],
                                    "destinationPrefixes": ["2001:db8::/64", "203.0.113.0/24"],
                                    "protocols": ["udp"],
                                },
                                "nextHop": {"vrf": "invalid-a"},
                            },
                            {
                                "trafficMatch": {
                                    "sourcePrefixes": ["198.51.100.0/24"],
                                    "destinationPrefixes": ["203.0.113.0/24"],
                                    "protocols": ["sctp"],
                                },
                                "nextHop": {"vrf": "invalid-a"},
                            }
                        ],
                    }
                },
            },
        }
    )
    netplan_document = load_document(
        {
            "apiVersion": "network.t-caas.telekom.com/v1alpha1",
            "kind": "NodeNetplanConfig",
            "metadata": {"name": "boundary-invalid-netplan"},
            "spec": {
                "interfaces": {
                    "eth7": {
                        "addresses": ["not-an-interface-address"],
                        "routes": [
                            {"to": "bad-prefix", "via": "192.0.2.1"},
                            {"to": "203.0.113.0/24", "via": "bad-next-hop"},
                        ],
                    }
                },
                "nameservers": ["9.9.9.9", "bad-ns"],
            },
        }
    )

    translator = VyosTranslator()
    network_result = translator.translate(network_document)
    netplan_result = translator.translate(netplan_document)
    combined = {
        "commands": [*network_result.commands, *netplan_result.commands],
        "warnings": [*network_result.warnings, *netplan_result.warnings],
        "unsupported": [*network_result.unsupported, *netplan_result.unsupported],
    }
    write_json(scenario_dir / "translation.json", combined)

    assert not any("not-a-prefix" in command for command in combined["commands"]), combined["commands"]
    assert "set system name-server '9.9.9.9'" in combined["commands"], combined["commands"]
    assert not any("bad-ns" in command for command in combined["commands"]), combined["commands"]
    assert any("static route prefix 'not-a-prefix' is invalid" in warning for warning in combined["warnings"]), combined["warnings"]
    assert any("static route next-hop 'not-an-ip' is invalid" in warning for warning in combined["warnings"]), combined["warnings"]
    assert any("uses a different address family" in warning for warning in combined["warnings"]), combined["warnings"]
    assert any("mixes IPv4 and IPv6 prefixes" in warning for warning in combined["warnings"]), combined["warnings"]
    assert any("address 'not-an-interface-address' is invalid" in warning for warning in combined["warnings"]), combined["warnings"]
    assert any("route destination 'bad-prefix' is invalid" in warning for warning in combined["warnings"]), combined["warnings"]
    assert any("route via 'bad-next-hop' is invalid" in warning for warning in combined["warnings"]), combined["warnings"]
    assert any("nameserver 'bad-ns' is invalid" in warning for warning in combined["warnings"]), combined["warnings"]
    assert any("no supported protocol" in warning for warning in combined["warnings"]), combined["warnings"]

    return {
        "scenario": "invalid-value-boundaries",
        "commandCount": len(combined["commands"]),
        "warningCount": len(combined["warnings"]),
        "unsupportedCount": len(combined["unsupported"]),
    }


class GuardClient:
    def __init__(self) -> None:
        self.calls = 0

    def configure_commands(self, commands: list[str]) -> dict:
        self.calls += 1
        return {"success": True, "operations": [{"success": True}]}


def scenario_zero_command_reconcile_boundary() -> dict:
    scenario_dir = ARTIFACT_DIR / "zero-command-reconcile-boundary"
    reset_dir(scenario_dir)
    state_file = scenario_dir / "state.json"
    status_file = scenario_dir / "status.json"

    document = load_document(
        {
            "apiVersion": "network.t-caas.telekom.com/v1alpha1",
            "kind": "NodeNetplanConfig",
            "metadata": {"name": "boundary-empty-netplan"},
            "spec": {},
        }
    )

    client = GuardClient()
    result = reconcile_documents(
        [document],
        VyosTranslator(),
        ReconcileState(),
        str(state_file),
        apply=True,
        client=client,
        status_file=str(status_file),
    )
    write_json(scenario_dir / "reconcile.json", result.to_dict())

    item = result.documents[0]
    assert result.command_count == 0, result.to_dict()
    assert result.apply_performed is False, result.to_dict()
    assert result.vyos_response is None, result.to_dict()
    assert client.calls == 0, client.calls
    assert item.command_count == 0, item.to_dict()
    assert item.action == "applied", item.to_dict()
    assert item.in_sync is True, item.to_dict()
    assert item.desired_revision.startswith("digest-"), item.to_dict()
    assert item.applied_revision == item.desired_revision, item.to_dict()

    saved = json.loads(state_file.read_text())
    write_json(scenario_dir / "saved-state.json", saved)
    state_entry = saved["documents"]["NodeNetplanConfig:default/boundary-empty-netplan"]
    assert state_entry["applied_revision"] == item.desired_revision, state_entry
    assert state_entry["command_count"] == 0, state_entry

    return {
        "scenario": "zero-command-reconcile-boundary",
        "commandCount": result.command_count,
        "applyPerformed": result.apply_performed,
        "desiredRevision": item.desired_revision,
    }


def scenario_malformed_structure_boundaries() -> dict:
    scenario_dir = ARTIFACT_DIR / "malformed-structure-boundaries"
    reset_dir(scenario_dir)

    cases = [
        {
            "name": "node-network-spec-not-object",
            "document": {
                "apiVersion": "network.t-caas.telekom.com/v1alpha1",
                "kind": "NodeNetworkConfig",
                "metadata": {"name": "bad-network"},
                "spec": [],
            },
            "expected": "NodeNetworkConfig.spec must be an object",
        },
        {
            "name": "policy-next-hop-not-object",
            "document": {
                "apiVersion": "network.t-caas.telekom.com/v1alpha1",
                "kind": "NodeNetworkConfig",
                "metadata": {"name": "bad-policy"},
                "spec": {
                    "localVRFs": {
                        "tenant-a": {
                            "table": 1000,
                            "policyRoutes": [
                                {
                                    "trafficMatch": {},
                                    "nextHop": [],
                                }
                            ],
                        }
                    }
                },
            },
            "expected": "nextHop must be an object",
        },
        {
            "name": "node-netplan-routes-not-list",
            "document": {
                "apiVersion": "network.t-caas.telekom.com/v1alpha1",
                "kind": "NodeNetplanConfig",
                "metadata": {"name": "bad-netplan"},
                "spec": {
                    "interfaces": {
                        "eth1": {
                            "addresses": ["192.0.2.1/24"],
                            "routes": {},
                        }
                    }
                },
            },
            "expected": "interface eth1.routes must be a list",
        },
    ]

    results: list[dict[str, str]] = []
    for case in cases:
        try:
            load_document(case["document"])
        except ValueError as exc:
            message = str(exc)
            assert case["expected"] in message, message
            results.append({"name": case["name"], "error": message})
        else:
            raise AssertionError(f"{case['name']} should have failed validation")

    write_json(scenario_dir / "results.json", {"cases": results})
    return {
        "scenario": "malformed-structure-boundaries",
        "caseCount": len(results),
    }


def scenario_desired_state_boundaries() -> dict:
    """Test NodeNetplanConfig.spec.desiredState (netplan native format) parsing."""
    scenario_dir = ARTIFACT_DIR / "desired-state-boundaries"
    reset_dir(scenario_dir)

    # --- wrapped: spec.desiredState.network ---
    doc_wrapped = load_document(
        {
            "apiVersion": "network.t-caas.telekom.com/v1alpha1",
            "kind": "NodeNetplanConfig",
            "metadata": {"name": "desired-state-wrapped"},
            "spec": {
                "desiredState": {
                    "network": {
                        "version": 2,
                        "ethernets": {
                            "eth1": {
                                "addresses": ["192.0.2.10/24"],
                                "routes": [{"to": "0.0.0.0/0", "via": "192.0.2.1"}],
                                "mtu": 9000,
                            },
                            "eth2": {"dhcp4": True},
                        },
                        "nameservers": {"addresses": ["1.1.1.1", "9.9.9.9"]},
                    }
                }
            },
        }
    )

    # --- unwrapped: spec.desiredState without network: key ---
    doc_unwrapped = load_document(
        {
            "apiVersion": "network.t-caas.telekom.com/v1alpha1",
            "kind": "NodeNetplanConfig",
            "metadata": {"name": "desired-state-unwrapped"},
            "spec": {
                "desiredState": {
                    "ethernets": {
                        "eth3": {
                            "addresses": ["198.51.100.5/24"],
                        }
                    },
                    "nameservers": ["8.8.8.8"],
                }
            },
        }
    )

    # --- bonds section ---
    doc_bond = load_document(
        {
            "apiVersion": "network.t-caas.telekom.com/v1alpha1",
            "kind": "NodeNetplanConfig",
            "metadata": {"name": "desired-state-bond"},
            "spec": {
                "desiredState": {
                    "network": {
                        "bonds": {
                            "bond0": {
                                "addresses": ["10.0.0.1/24"],
                                "dhcp6": True,
                            }
                        }
                    }
                }
            },
        }
    )

    # --- empty desiredState ---
    doc_empty = load_document(
        {
            "apiVersion": "network.t-caas.telekom.com/v1alpha1",
            "kind": "NodeNetplanConfig",
            "metadata": {"name": "desired-state-empty"},
            "spec": {"desiredState": {}},
        }
    )

    translator = VyosTranslator()
    results = {}
    for doc in (doc_wrapped, doc_unwrapped, doc_bond, doc_empty):
        r = translator.translate(doc)
        results[doc.metadata.name] = {
            "commands": r.commands,
            "warnings": r.warnings,
        }

    write_json(scenario_dir / "results.json", results)

    # wrapped assertions
    wrapped = results["desired-state-wrapped"]
    assert "set interfaces ethernet eth1 address '192.0.2.10/24'" in wrapped["commands"], wrapped
    assert "set interfaces ethernet eth1 mtu '9000'" in wrapped["commands"], wrapped
    assert "set protocols static route 0.0.0.0/0 next-hop '192.0.2.1'" in wrapped["commands"], wrapped
    assert "set interfaces ethernet eth2 address 'dhcp'" in wrapped["commands"], wrapped
    assert "set system name-server '1.1.1.1'" in wrapped["commands"], wrapped
    assert "set system name-server '9.9.9.9'" in wrapped["commands"], wrapped

    # unwrapped assertions
    unwrapped = results["desired-state-unwrapped"]
    assert "set interfaces ethernet eth3 address '198.51.100.5/24'" in unwrapped["commands"], unwrapped
    assert "set system name-server '8.8.8.8'" in unwrapped["commands"], unwrapped

    # bond assertions
    bond = results["desired-state-bond"]
    assert "set interfaces bonding bond0 address '10.0.0.1/24'" in bond["commands"], bond
    assert "set interfaces bonding bond0 address 'dhcp6'" in bond["commands"], bond

    # empty produces no commands
    empty = results["desired-state-empty"]
    assert empty["commands"] == [], empty

    return {
        "scenario": "desired-state-boundaries",
        "docCount": 4,
        "wrappedCommandCount": len(results["desired-state-wrapped"]["commands"]),
        "unwrappedCommandCount": len(results["desired-state-unwrapped"]["commands"]),
        "bondCommandCount": len(results["desired-state-bond"]["commands"]),
        "emptyCommandCount": len(results["desired-state-empty"]["commands"]),
    }


def scenario_bgp_delete_regex_quotes() -> dict:
    """BGP delete consolidation with VRF/neighbor names containing escaped quotes.

    Verifies that _BGP_NEIGHBOR_RE and _SCALAR_LEAF_RE correctly match commands
    where VRF or neighbor names contain an escaped single quote (\\').
    """
    scenario_dir = ARTIFACT_DIR / "bgp-delete-regex-quotes"
    reset_dir(scenario_dir)

    # Build the exact command strings that the translator would produce.
    vrf_plain   = "vrf name 'tenant-a' protocols bgp"
    vrf_quoted  = r"vrf name 'tenant\'s-edge' protocols bgp"
    peer_plain  = "192.0.2.1"
    peer_quoted = r"peer\'s-addr"

    cmds_plain = [
        f"set {vrf_plain} system-as '65001'",
        f"set {vrf_plain} neighbor '{peer_plain}' remote-as '65002'",
        f"set {vrf_plain} neighbor '{peer_plain}' address-family ipv4-unicast",
        f"set {vrf_plain} neighbor '{peer_plain}' password 'secret'",
    ]
    cmds_quoted = [
        f"set {vrf_quoted} system-as '65010'",
        f"set {vrf_quoted} neighbor '{peer_quoted}' remote-as '65020'",
        f"set {vrf_quoted} neighbor '{peer_quoted}' address-family ipv6-unicast",
        f"set {vrf_quoted} neighbor '{peer_quoted}' timers keepalive '30'",
        f"set {vrf_quoted} neighbor '{peer_quoted}' timers holdtime '90'",
    ]

    results = {}
    for label, cmds in (("plain", cmds_plain), ("quoted", cmds_quoted)):
        neighbor_matches = [bool(_BGP_NEIGHBOR_RE.match(c)) for c in cmds]
        scalar_matches   = [bool(_SCALAR_LEAF_RE.match(c)) for c in cmds]
        results[label] = {
            "commands": cmds,
            "neighbor_match": neighbor_matches,
            "scalar_match": scalar_matches,
        }

    write_json(scenario_dir / "results.json", results)

    # plain names — sanity check
    assert results["plain"]["neighbor_match"] == [False, True, True, True], results["plain"]
    assert results["plain"]["scalar_match"]   == [True,  True, False, True], results["plain"]

    # quoted names — the actual bug fix
    assert results["quoted"]["neighbor_match"] == [False, True, True, True, True], results["quoted"]
    assert results["quoted"]["scalar_match"]   == [True,  True, False, True, True], results["quoted"]

    return {
        "scenario": "bgp-delete-regex-quotes",
        "plainCmds": len(cmds_plain),
        "quotedCmds": len(cmds_quoted),
    }


def scenario_large_topology() -> dict:
    """10 VRFs × 10 BGP peers × 2 VLAN subinterfaces × 100 static routes per VRF.

    Validates that the translator and reconcile layer handle a realistic
    large-scale topology without crashes, command duplication, or digest
    instability.
    """
    scenario_dir = ARTIFACT_DIR / "large-topology"
    reset_dir(scenario_dir)
    state_file  = scenario_dir / "state.json"
    status_file = scenario_dir / "status.json"

    N_VRFS      = 10
    N_PEERS     = 10
    N_VLANS     = 2
    N_ROUTES    = 100

    local_vrfs: dict = {}
    for v in range(N_VRFS):
        vrf_name = f"tenant-{v:02d}"
        table    = 1000 + v

        static_routes = []
        for r in range(N_ROUTES):
            prefix   = f"10.{v}.{r // 256}.{r % 256}/32"
            next_hop = f"192.168.{v}.1"
            static_routes.append({"prefix": prefix, "nextHop": {"address": next_hop}})

        bgp_peers = []
        for p in range(N_PEERS):
            bgp_peers.append({
                "address": f"172.16.{v}.{p + 1}",
                "remoteASN": 65000 + v * 10 + p,
                "addressFamilies": ["ipv4-unicast"],
            })

        interfaces = []
        for vlan in range(N_VLANS):
            interfaces.append(f"eth{v}.{100 + vlan}")

        local_vrfs[vrf_name] = {
            "table": table,
            "interfaces": interfaces,
            "staticRoutes": static_routes,
            "bgpPeers": bgp_peers,
            "localASN": 65000 + v,
        }

    document = load_document({
        "apiVersion": "network.t-caas.telekom.com/v1alpha1",
        "kind": "NodeNetworkConfig",
        "metadata": {"name": "large-topology-node"},
        "spec": {
            "revision": "large-topo-r1",
            "localVRFs": local_vrfs,
        },
    })

    translator = VyosTranslator()
    result = translator.translate(document)
    write_json(scenario_dir / "translation.json", {
        "commandCount": len(result.commands),
        "warningCount": len(result.warnings),
        "unsupportedCount": len(result.unsupported),
        "commands": result.commands,
    })

    # No crashes, expected command volume:
    # at minimum table + routes + remote-as + address-family per VRF
    expected_min_commands = N_VRFS * (1 + N_ROUTES + N_PEERS + N_PEERS)
    assert len(result.commands) >= expected_min_commands, (
        f"expected >= {expected_min_commands} commands, got {len(result.commands)}"
    )
    # All 100 routes present for VRF 0
    assert sum(1 for c in result.commands if "10.0." in c and "next-hop" in c) == N_ROUTES, (
        "route count mismatch for tenant-00"
    )

    # No duplicate commands
    assert len(result.commands) == len(set(result.commands)), "duplicate commands detected"

    # Reconcile — first pass applies
    client = GuardClient()
    rec1 = reconcile_documents(
        [document],
        translator,
        ReconcileState(),
        str(state_file),
        apply=True,
        client=client,
        status_file=str(status_file),
    )
    write_json(scenario_dir / "reconcile-1.json", rec1.to_dict())
    assert rec1.apply_performed, "first reconcile should apply"
    assert client.calls == 1, client.calls

    # Reconcile — second pass is a noop (digest stable)
    state2 = ReconcileState.load(str(state_file))
    rec2 = reconcile_documents(
        [document],
        translator,
        state2,
        str(state_file),
        apply=True,
        client=client,
        status_file=str(status_file),
    )
    write_json(scenario_dir / "reconcile-2.json", rec2.to_dict())
    assert not rec2.apply_performed, "second reconcile should be a noop"
    assert client.calls == 1, f"client called again on noop: {client.calls}"

    return {
        "scenario": "large-topology",
        "vrfCount": N_VRFS,
        "peersPerVrf": N_PEERS,
        "vlansPerVrf": N_VLANS,
        "routesPerVrf": N_ROUTES,
        "totalCommands": len(result.commands),
        "warnings": len(result.warnings),
        "noop": not rec2.apply_performed,
    }


def scenario_cluster_scoped_url_routing() -> dict:
    """Verify that _resource_url() produces correct paths for namespace-scoped and cluster-scoped modes."""
    scenario_dir = ARTIFACT_DIR / "cluster-scoped-url-routing"
    reset_dir(scenario_dir)

    conn = KubeConnection(server="https://k8s.test:6443", verify_tls=False)
    client = KubeDocumentClient(connection=conn)
    resource = CustomResourceSpec(
        api_version="network.t-caas.telekom.com/v1alpha1",
        kind="NodeNetworkConfig",
        plural="nodenetworkconfigs",
    )

    # Namespace-scoped: URL must contain /namespaces/<ns>/
    url_ns = client._resource_url(resource, namespace="default", cluster_scoped=False)
    assert "/namespaces/default/" in url_ns, url_ns
    assert url_ns.endswith("/nodenetworkconfigs"), url_ns

    # Cluster-scoped: URL must NOT contain /namespaces/
    url_cluster = client._resource_url(resource, namespace="default", cluster_scoped=True)
    assert "/namespaces/" not in url_cluster, url_cluster
    assert url_cluster.endswith("/nodenetworkconfigs"), url_cluster

    # namespace=None with cluster_scoped=False behaves like cluster-scoped
    url_none_ns = client._resource_url(resource, namespace=None, cluster_scoped=False)
    assert "/namespaces/" not in url_none_ns, url_none_ns

    # Non-default namespace is preserved in namespace-scoped mode
    url_other_ns = client._resource_url(resource, namespace="tenant-a", cluster_scoped=False)
    assert "/namespaces/tenant-a/" in url_other_ns, url_other_ns

    # Both modes share the same base API group path
    assert "network.t-caas.telekom.com" in url_ns
    assert "network.t-caas.telekom.com" in url_cluster

    write_json(scenario_dir / "results.json", {
        "url_namespace_scoped": url_ns,
        "url_cluster_scoped": url_cluster,
        "url_namespace_none": url_none_ns,
        "url_other_namespace": url_other_ns,
    })

    return {
        "scenario": "cluster-scoped-url-routing",
        "url_namespace_scoped": url_ns,
        "url_cluster_scoped": url_cluster,
    }


def scenario_cra_status_conditions() -> dict:
    """Verify Reconciling / Degraded / Available conditions for all phase states."""
    scenario_dir = ARTIFACT_DIR / "cra-status-conditions"
    reset_dir(scenario_dir)

    now = "2026-03-23T00:00:00+00:00"

    def make_state(**kwargs) -> ReconcileState:
        s = ReconcileState()
        s.documents["test/doc"] = DocumentState(
            key="test/doc",
            api_version="network.t-caas.telekom.com/v1alpha1",
            kind="NodeNetworkConfig",
            name="doc",
            namespace="default",
            last_seen_at=now,
            **kwargs,
        )
        return s

    # InSync: desired == applied
    state_in_sync = make_state(
        desired_revision="r1", desired_digest="d1",
        applied_revision="r1", applied_digest="d1",
        last_result="applied",
    )
    # PendingApply: desired set, applied absent
    state_pending = make_state(
        desired_revision="r2", desired_digest="d2",
        applied_revision=None, applied_digest=None,
        last_result="pending-apply",
    )
    # Drifted: desired != applied (both set)
    state_drifted = make_state(
        desired_revision="r3", desired_digest="d3",
        applied_revision="r2", applied_digest="d2",
        last_result="pending-apply",
    )
    # Error: last_error set
    state_error = make_state(
        desired_revision="r4", desired_digest="d4",
        applied_revision=None, applied_digest=None,
        last_result="apply-failed",
        last_error="commit failed",
    )

    results = {}
    for label, state in (
        ("in_sync", state_in_sync),
        ("pending", state_pending),
        ("drifted", state_drifted),
        ("error", state_error),
    ):
        report = build_status_report(state, now=now)
        doc = report.documents[0]
        condition_map = {c.type: c.status for c in doc.conditions}
        results[label] = {"phase": doc.phase, "conditions": condition_map}

    write_json(scenario_dir / "results.json", results)

    # InSync: Available=True, Reconciling=False, Degraded=False
    assert results["in_sync"]["phase"] == "InSync", results["in_sync"]
    assert results["in_sync"]["conditions"]["Available"] == "True", results["in_sync"]
    assert results["in_sync"]["conditions"]["Reconciling"] == "False", results["in_sync"]
    assert results["in_sync"]["conditions"]["Degraded"] == "False", results["in_sync"]

    # PendingApply: Available=False, Reconciling=True, Degraded=False
    assert results["pending"]["phase"] == "PendingApply", results["pending"]
    assert results["pending"]["conditions"]["Available"] == "False", results["pending"]
    assert results["pending"]["conditions"]["Reconciling"] == "True", results["pending"]
    assert results["pending"]["conditions"]["Degraded"] == "False", results["pending"]

    # Drifted: Available=False, Reconciling=True, Degraded=False
    assert results["drifted"]["phase"] == "Drifted", results["drifted"]
    assert results["drifted"]["conditions"]["Reconciling"] == "True", results["drifted"]
    assert results["drifted"]["conditions"]["Available"] == "False", results["drifted"]

    # Error: Available=False, Reconciling=False, Degraded=True
    assert results["error"]["phase"] == "Error", results["error"]
    assert results["error"]["conditions"]["Degraded"] == "True", results["error"]
    assert results["error"]["conditions"]["Available"] == "False", results["error"]
    assert results["error"]["conditions"]["Reconciling"] == "False", results["error"]

    return {
        "scenario": "cra-status-conditions",
        "phases_tested": list(results.keys()),
        "condition_types": list(results["in_sync"]["conditions"].keys()),
    }


def main() -> int:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    summaries = [
        scenario_interface_boundaries(),
        scenario_static_and_policy_boundaries(),
        scenario_bgp_boundaries(),
        scenario_netplan_boundaries(),
        scenario_invalid_value_boundaries(),
        scenario_zero_command_reconcile_boundary(),
        scenario_malformed_structure_boundaries(),
        scenario_desired_state_boundaries(),
        scenario_bgp_delete_regex_quotes(),
        scenario_large_topology(),
        scenario_cluster_scoped_url_routing(),
        scenario_cra_status_conditions(),
    ]
    summary = {
        "scenarioCount": len(summaries),
        "scenarios": summaries,
        "artifactDir": str(ARTIFACT_DIR),
    }
    write_json(ARTIFACT_DIR / "summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
