#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import random
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_ROOT = REPO_ROOT
if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))

from hbr_vyos_adapter.loader import load_documents
from hbr_vyos_adapter.models import load_document
from hbr_vyos_adapter.reconcile import reconcile_documents
from hbr_vyos_adapter.state import ReconcileState
from hbr_vyos_adapter.translator import VyosTranslator


ARTIFACT_DIR = REPO_ROOT / "artifacts" / "private" / "hbr-fuzz-local"
EXAMPLE_NETWORK = ADAPTER_ROOT / "examples/node-network-config.json"
EXAMPLE_NETPLAN = ADAPTER_ROOT / "examples/node-netplan-config.json"


class RecordingVyosClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def configure_commands(self, commands: list[str]) -> dict:
        self.calls.append(list(commands))
        return {"success": True, "operations": [{"success": True, "count": len(commands)}]}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local fuzz harness for the HBR VyOS adapter")
    parser.add_argument("command", choices=("run",), nargs="?", default="run")
    parser.add_argument("--iterations", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20260320)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = run_fuzz(iterations=args.iterations, seed=args.seed)
    print(json.dumps(summary, indent=2))
    return 0


def run_fuzz(*, iterations: int, seed: int) -> dict:
    rng = random.Random(seed)
    reset_dir(ARTIFACT_DIR)
    translator = VyosTranslator()
    network_base = load_documents(EXAMPLE_NETWORK)[0]
    netplan_base = load_documents(EXAMPLE_NETPLAN)[0]

    case_summaries: list[dict] = []
    total_warning_count = 0
    total_unsupported_count = 0
    total_command_count = 0
    max_command_count = 0

    for case_index in range(1, iterations + 1):
        case_dir = ARTIFACT_DIR / f"case-{case_index:03d}"
        case_dir.mkdir(parents=True, exist_ok=True)

        network_raw = mutate_network_config(network_base.raw, rng, case_index)
        netplan_raw = mutate_netplan_config(netplan_base.raw, rng, case_index)

        write_json(case_dir / "node-network-config.json", network_raw)
        write_json(case_dir / "node-netplan-config.json", netplan_raw)

        try:
            documents = [load_document(network_raw), load_document(netplan_raw)]

            pending = reconcile_documents(
                documents,
                translator,
                ReconcileState(),
                str(case_dir / "pending-state.json"),
                apply=False,
                status_file=str(case_dir / "pending-status.json"),
            )

            applied_state = ReconcileState()
            client = RecordingVyosClient()
            applied = reconcile_documents(
                documents,
                translator,
                applied_state,
                str(case_dir / "applied-state.json"),
                apply=True,
                client=client,
                status_file=str(case_dir / "applied-status.json"),
            )

            repeated = reconcile_documents(
                documents,
                translator,
                applied_state,
                str(case_dir / "repeat-state.json"),
                apply=False,
                status_file=str(case_dir / "repeat-status.json"),
            )
        except Exception as exc:
            failure = {
                "case": case_index,
                "seed": seed,
                "error": f"{type(exc).__name__}: {exc}",
            }
            write_json(case_dir / "failure.json", failure)
            raise

        ensure_pending_invariants(case_index, pending)
        ensure_applied_invariants(case_index, applied, client)
        ensure_repeat_invariants(case_index, repeated)

        warning_count = sum(item.warning_count for item in pending.documents)
        unsupported_count = sum(item.unsupported_count for item in pending.documents)
        total_warning_count += warning_count
        total_unsupported_count += unsupported_count
        total_command_count += pending.command_count
        max_command_count = max(max_command_count, pending.command_count)

        case_summary = {
            "case": case_index,
            "pending_command_count": pending.command_count,
            "warning_count": warning_count,
            "unsupported_count": unsupported_count,
            "apply_call_count": len(client.calls),
            "repeat_pending_command_count": repeated.command_count,
        }
        case_summaries.append(case_summary)
        write_json(case_dir / "summary.json", case_summary)

    summary = {
        "seed": seed,
        "iterations": iterations,
        "caseCount": len(case_summaries),
        "totalWarningCount": total_warning_count,
        "totalUnsupportedCount": total_unsupported_count,
        "totalCommandCount": total_command_count,
        "maxCommandCount": max_command_count,
        "artifactDir": str(ARTIFACT_DIR),
        "cases": case_summaries,
    }
    write_json(ARTIFACT_DIR / "summary.json", summary)
    return summary


def ensure_pending_invariants(case_index: int, result) -> None:
    assert len(result.documents) == 2, f"case {case_index}: expected 2 documents"
    assert result.status_report["documentCount"] == 2, f"case {case_index}: bad status count"
    for document in result.documents:
        assert len(document.desired_digest) == 64, f"case {case_index}: bad digest length"
        assert document.action == "pending-apply", f"case {case_index}: expected pending-apply"
        assert document.in_sync is False, f"case {case_index}: pending result should not be in sync"


def ensure_applied_invariants(case_index: int, result, client: RecordingVyosClient) -> None:
    for document in result.documents:
        assert document.in_sync is True, f"case {case_index}: apply did not converge"
        assert document.action == "applied", f"case {case_index}: expected applied action"
        assert document.applied_digest == document.desired_digest, (
            f"case {case_index}: applied digest mismatch"
        )
    if result.command_count > 0:
        assert len(client.calls) == 1, f"case {case_index}: expected one VyOS apply call"
    else:
        assert len(client.calls) == 0, f"case {case_index}: zero-command case should not call VyOS"


def ensure_repeat_invariants(case_index: int, result) -> None:
    assert result.command_count == 0, f"case {case_index}: repeat reconcile should be clean"
    for document in result.documents:
        assert document.in_sync is True, f"case {case_index}: repeat reconcile drifted"
        assert document.action == "noop", f"case {case_index}: repeat reconcile should noop"


def mutate_network_config(raw: dict, rng: random.Random, case_index: int) -> dict:
    payload = copy.deepcopy(raw)
    metadata = payload.setdefault("metadata", {})
    spec = payload.setdefault("spec", {})
    metadata["generation"] = case_index
    metadata["resourceVersion"] = f"{case_index}"
    spec["revision"] = f"fuzz-{case_index:04d}-{rng.randint(100, 999)}"

    local_vrfs = spec.setdefault("localVRFs", {})
    tenant = local_vrfs.setdefault("tenant-a", {})
    tenant["table"] = rng.choice([1, 10, 100, 1000, 4095])
    tenant["interfaces"] = random_interfaces(rng)
    tenant["localASN"] = rng.choice([65001, 65010, 65040, 65100])
    tenant["bgpPeers"] = random_bgp_peers(rng)
    tenant["policyRoutes"] = random_policy_routes(rng)
    tenant["staticRoutes"] = random_static_routes(rng)

    fabric_vrfs = spec.setdefault("fabricVRFs", {})
    fabric = fabric_vrfs.setdefault("fabric-a", {})
    fabric["table"] = rng.choice([1100, 1200, 2000, 3000])
    fabric["localASN"] = rng.choice([65100, 65101, 65200])
    fabric["bgpPeers"] = random_bgp_peers(rng)
    fabric["staticRoutes"] = random_static_routes(rng)

    if rng.random() < 0.35:
        spec["layer2s"] = {f"l2-{case_index}": {"vlan": rng.randint(100, 4000)}}
    else:
        spec.pop("layer2s", None)

    return payload


def mutate_netplan_config(raw: dict, rng: random.Random, case_index: int) -> dict:
    payload = copy.deepcopy(raw)
    metadata = payload.setdefault("metadata", {})
    spec = payload.setdefault("spec", {})
    metadata["generation"] = case_index
    metadata["resourceVersion"] = f"net-{case_index}"

    interface_count = rng.randint(1, 2)
    interfaces: dict[str, dict] = {}
    for index in range(interface_count):
        name = "eth1" if index == 0 else rng.choice(["eth2", "eth3"])
        interfaces[name] = {
            "addresses": random_interface_addresses(rng),
            "routes": random_netplan_routes(rng),
        }
    spec["interfaces"] = interfaces
    spec["nameservers"] = random_nameservers(rng)
    return payload


def random_interfaces(rng: random.Random) -> list[str]:
    choices = ["eth1", "bond1", "dummy0", "vxlan1", "lo", "myst0"]
    rng.shuffle(choices)
    return sorted(choices[: rng.randint(1, 3)])


def random_bgp_peers(rng: random.Random) -> list[dict]:
    peers: list[dict] = []
    for _ in range(rng.randint(1, 3)):
        is_v6 = rng.random() < 0.45
        peer = {
            "address": random_ipv6_host(rng) if is_v6 else random_ipv4_host(rng),
            "remoteASN": rng.choice([65010, 65020, 65100, 65200]),
        }
        if rng.random() < 0.6:
            peer["addressFamilies"] = rng.sample(
                ["ipv4", "ipv4-unicast", "ipv6", "ipv6-unicast", "l2vpn-evpn"],
                k=rng.randint(1, 2),
            )
        if rng.random() < 0.5:
            peer["updateSource"] = rng.choice(["lo", "eth1", "2001:db8::1"])
        if rng.random() < 0.4:
            peer["ebgpMultihop"] = rng.randint(1, 4)
        if rng.random() < 0.2:
            peer["holdTime"] = rng.randint(3, 90)
        # Generate structured BGP filters ~40% of the time
        if rng.random() < 0.4:
            af_key = "ipv6" if is_v6 else "ipv4"
            peer[af_key] = {}
            if rng.random() < 0.7:
                peer[af_key]["importFilter"] = random_bgp_filter(rng, is_v6)
            if rng.random() < 0.5:
                peer[af_key]["exportFilter"] = random_bgp_filter(rng, is_v6)
        peers.append(peer)
    return peers


def random_bgp_filter(rng: random.Random, is_v6: bool) -> dict:
    default_action_type = rng.choice(["accept", "reject", "next"])
    bgp_filter: dict = {
        "defaultAction": {"type": default_action_type},
    }
    items: list[dict] = []
    for _ in range(rng.randint(0, 3)):
        item: dict = {
            "action": {"type": rng.choice(["accept", "reject"])},
            "matcher": {},
        }
        # Prefix matcher
        if rng.random() < 0.6:
            family = 6 if is_v6 else 4
            prefix = random_prefix(rng, family)
            matcher: dict = {"prefix": prefix}
            if rng.random() < 0.4:
                matcher["ge"] = rng.randint(8, 32)
            if rng.random() < 0.4:
                matcher["le"] = rng.randint(8, 32)
            item["matcher"]["prefix"] = matcher
        # Community matcher
        if rng.random() < 0.3:
            asn = rng.choice([65000, 65010, 65100])
            val = rng.randint(1, 999)
            item["matcher"]["bgpCommunity"] = {
                "community": f"{asn}:{val}",
                "exactMatch": rng.random() < 0.5,
            }
        # Route modifications
        if rng.random() < 0.3:
            modify: dict = {}
            if rng.random() < 0.5:
                modify["addCommunities"] = [f"65000:{rng.randint(1, 999)}"]
                modify["additiveCommunities"] = rng.random() < 0.5
            if rng.random() < 0.3:
                modify["removeAllCommunities"] = True
            if modify:
                item["action"]["modifyRoute"] = modify
        items.append(item)
    if items:
        bgp_filter["items"] = items
    return bgp_filter


def random_policy_routes(rng: random.Random) -> list[dict]:
    routes: list[dict] = []
    for _ in range(rng.randint(1, 3)):
        family = 6 if rng.random() < 0.35 else 4
        route = {
            "trafficMatch": {
                "interface": rng.choice(["eth1", "bond1", None]),
                "sourcePrefixes": [random_prefix(rng, family)],
                "destinationPrefixes": [random_prefix(rng, family)],
                "protocols": [rng.choice(["tcp", "udp", "icmp", "sctp"])],
            },
            "nextHop": {},
        }
        if rng.random() < 0.5:
            route["trafficMatch"]["sourcePorts"] = [str(rng.randint(1, 65535))]
        if rng.random() < 0.5:
            route["trafficMatch"]["destinationPorts"] = [str(rng.randint(1, 65535))]
        if rng.random() < 0.5:
            route["nextHop"]["vrf"] = rng.choice(["tenant-a", "fabric-a"])
        if rng.random() < 0.4:
            route["nextHop"]["address"] = random_ipv6_host(rng) if family == 6 else random_ipv4_host(rng)
        routes.append(route)
    return routes


def random_static_routes(rng: random.Random) -> list[dict]:
    routes: list[dict] = []
    for _ in range(rng.randint(1, 3)):
        family = 6 if rng.random() < 0.35 else 4
        route = {
            "prefix": random_prefix(rng, family),
            "nextHop": {
                "address": random_ipv6_host(rng) if family == 6 else random_ipv4_host(rng)
            },
        }
        if rng.random() < 0.2:
            route["prefix"] = ""
        routes.append(route)
    return routes


def random_interface_addresses(rng: random.Random) -> list[str]:
    addresses = [f"{random_ipv4_host(rng)}/24"]
    if rng.random() < 0.4:
        addresses.append(f"{random_ipv6_host(rng)}/64")
    return addresses


def random_netplan_routes(rng: random.Random) -> list[dict]:
    routes: list[dict] = []
    for _ in range(rng.randint(1, 3)):
        family = 6 if rng.random() < 0.35 else 4
        if family == 6 and rng.random() < 0.4:
            to = "::/0"
        elif family == 4 and rng.random() < 0.4:
            to = "0.0.0.0/0"
        else:
            to = random_prefix(rng, family)
        route = {"to": to}
        if rng.random() < 0.85:
            route["via"] = random_ipv6_host(rng) if family == 6 else random_ipv4_host(rng)
        routes.append(route)
    return routes


def random_nameservers(rng: random.Random) -> list[str]:
    pool = [
        "1.1.1.1",
        "8.8.8.8",
        "9.9.9.9",
        "2606:4700:4700::1111",
        "2001:4860:4860::8888",
    ]
    rng.shuffle(pool)
    return pool[: rng.randint(1, 3)]


def random_prefix(rng: random.Random, family: int) -> str:
    if family == 6:
        base = [0x2001, 0x0DB8, rng.randint(0, 0xFFFF), rng.randint(0, 0xFFFF)]
        prefix = ":".join(f"{part:x}" for part in base)
        return f"{prefix}::/{rng.choice([48, 56, 64, 80, 96])}"
    octets = [rng.randint(1, 223), rng.randint(0, 255), rng.randint(0, 255)]
    return f"{octets[0]}.{octets[1]}.{octets[2]}.0/{rng.choice([16, 24, 25, 26, 27, 28, 32])}"


def random_ipv4_host(rng: random.Random) -> str:
    return ".".join(str(rng.randint(1 if index == 0 else 0, 254)) for index in range(4))


def random_ipv6_host(rng: random.Random) -> str:
    return ":".join(f"{rng.randint(0, 0xFFFF):x}" for _ in range(8))


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
