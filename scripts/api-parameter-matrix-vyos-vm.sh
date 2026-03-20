#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${repo_root}/.venv/bin/python3"
if [[ ! -x "$python_bin" ]]; then
  python_bin="python3"
fi

VENUS_HOST="${VENUS_HOST:-user@venus.example.test}"
REMOTE_REPO="${REMOTE_REPO:-/home/user/cloud-connector-routing-adapter}"
REMOTE_KUBECTL="${REMOTE_KUBECTL:-/var/lib/rancher/rke2/bin/kubectl}"
REMOTE_KUBECONFIG="${REMOTE_KUBECONFIG:-/etc/rancher/rke2/rke2.yaml}"
VYOS_URL="${VYOS_URL:-https://vyos.example.test}"
VYOS_API_KEY="${VYOS_API_KEY:-replace-with-real-api-key}"
VYOS_TIMEOUT="${VYOS_TIMEOUT:-90}"
TUNNEL_PORT="${TUNNEL_PORT:-16443}"
TEST_NAMESPACE="${TEST_NAMESPACE:-hbr-api-matrix}"
CYCLES="${CYCLES:-2}"
LOCAL_WORKDIR="${LOCAL_WORKDIR:-/tmp/hbr-api-matrix}"
REMOTE_WORKDIR="${REMOTE_WORKDIR:-/tmp/hbr-api-matrix}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$repo_root/artifacts/hbr-api-matrix}"
LOCAL_KUBECONFIG="${LOCAL_WORKDIR}/venus-rke2.yaml"
BASE_NAMESERVER="${BASE_NAMESERVER:-192.0.2.53}"

tunnel_pid=""

usage() {
  cat <<'EOF'
Usage:
  scripts/api-parameter-matrix-vyos-vm.sh run

Environment:
  CYCLES            Number of full API-matrix cycles to run (default: 2)
  TEST_NAMESPACE    Dedicated Kubernetes namespace for the matrix run (default: hbr-api-matrix)
  VENUS_HOST        SSH target for the lab host (default: user@venus.example.test)
  REMOTE_REPO       repo path on the lab host (default: /home/user/cloud-connector-routing-adapter)
  REMOTE_KUBECTL    kubectl path on Venus (default: /var/lib/rancher/rke2/bin/kubectl)
  REMOTE_KUBECONFIG RKE2 kubeconfig path on Venus (default: /etc/rancher/rke2/rke2.yaml)
  VYOS_URL          VyOS API URL (default: https://vyos.example.test)
  VYOS_API_KEY      VyOS API key (default: replace-with-real-api-key)
  VYOS_TIMEOUT      VyOS API timeout in seconds for controller calls (default: 90)
  TUNNEL_PORT       Local forwarded Kubernetes API port (default: 16443)
EOF
}

log() {
  printf '\n==> %s\n' "$*"
}

remote_run() {
  ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
    "$VENUS_HOST" "$1"
}

remote_kubectl() {
  remote_run "${REMOTE_KUBECTL} --kubeconfig ${REMOTE_KUBECONFIG} $1"
}

check_venus_access() {
  log "Checking SSH access to Venus"
  ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
    "$VENUS_HOST" "printf 'venus-ok\n'" >/dev/null
}

cleanup() {
  if [[ -n "$tunnel_pid" ]]; then
    kill "$tunnel_pid" >/dev/null 2>&1 || true
    wait "$tunnel_pid" 2>/dev/null || true
  fi
}

trap cleanup EXIT

pick_tunnel_port() {
  local requested_port="$1"
  "$python_bin" - "$requested_port" <<'PY'
import socket
import sys

requested = int(sys.argv[1])

def can_bind(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()

if can_bind(requested):
    print(requested)
else:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
    sock.close()
PY
}

sync_adapter() {
  log "Syncing adapter files to Venus"
  rsync -a \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '*.egg-info' \
     "${VENUS_HOST}:${REMOTE_REPO}/"
}

prepare_cluster() {
  log "Preparing the Venus RKE2 cluster for the API matrix"
  remote_run "mkdir -p ${REMOTE_WORKDIR}"
  remote_kubectl "create namespace ${TEST_NAMESPACE} --dry-run=client -o yaml | ${REMOTE_KUBECTL} --kubeconfig ${REMOTE_KUBECONFIG} apply -f -"
  remote_kubectl \
    "apply -f ${REMOTE_REPO}/k8s/crds/node-network-config-crd.yaml \
     -f ${REMOTE_REPO}/k8s/crds/node-netplan-config-crd.yaml"
  remote_kubectl \
    "delete nodenetworkconfigs,nodenetplanconfigs --all -n ${TEST_NAMESPACE} --ignore-not-found=true"
}

prepare_local_kubeconfig() {
  log "Copying the Venus RKE2 kubeconfig locally"
  mkdir -p "$LOCAL_WORKDIR" "$ARTIFACT_DIR"
  scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    "${VENUS_HOST}:${REMOTE_KUBECONFIG}" "$LOCAL_KUBECONFIG"
  "$python_bin" - "$LOCAL_KUBECONFIG" "$TUNNEL_PORT" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
port = sys.argv[2]
text = path.read_text()
text = text.replace("https://127.0.0.1:6443", f"https://127.0.0.1:{port}")
path.write_text(text)
PY
}

start_tunnel() {
  local requested_port="$TUNNEL_PORT"
  TUNNEL_PORT="$(pick_tunnel_port "$TUNNEL_PORT")"
  if [[ "$TUNNEL_PORT" != "$requested_port" ]]; then
    log "Local port ${requested_port} is busy, using tunnel port ${TUNNEL_PORT} instead"
    prepare_local_kubeconfig
  fi
  log "Opening a local tunnel to the Venus RKE2 API"
  ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new \
    -N -L "${TUNNEL_PORT}:127.0.0.1:6443" "$VENUS_HOST" &
  tunnel_pid="$!"
  sleep 1
  if ! kill -0 "$tunnel_pid" >/dev/null 2>&1; then
    wait "$tunnel_pid" 2>/dev/null || true
    echo "E: failed to establish the Kubernetes API tunnel on 127.0.0.1:${TUNNEL_PORT}" >&2
    exit 1
  fi
}

api_save() {
  curl -sk --connect-timeout 5 \
    --form 'data={"op":"save"}' \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/config-file" >/dev/null
}

generate_case_files() {
  local cycle="$1"
  local case_index="$2"
  local case_name="$3"
  local case_dir="${LOCAL_WORKDIR}/cycle-${cycle}-${case_name}"
  mkdir -p "$case_dir"
  "$python_bin" - "$case_dir" "$cycle" "$case_index" "$case_name" "$TEST_NAMESPACE" "$BASE_NAMESERVER" <<'PY'
import json
from pathlib import Path
import sys

case_dir = Path(sys.argv[1])
cycle = int(sys.argv[2])
case_index = int(sys.argv[3])
case_name = sys.argv[4]
namespace = sys.argv[5]
base_nameserver = sys.argv[6]
safe_case_name = case_name.replace("_", "-")

resource_name = f"vyos-api-matrix-{cycle}-{safe_case_name}"
vrf_name = f"api-{cycle}-{case_index}"
table = 2000 + cycle * 100 + case_index
base_v4 = f"198.51.{cycle}.{case_index}/32"
base_v6 = f"2001:db8:{cycle}:{case_index}::/64"
base_v4_next_hop = f"192.0.2.{20 + case_index}"
base_v6_next_hop = f"2001:db8:ffff::{20 + case_index}"
extra_nameserver = "9.9.9.9"
third_nameserver = "1.1.1.1"
policy_source = f"10.{cycle}.{case_index}.0/24"
policy_destination = f"172.{16 + cycle}.{case_index}.0/24"
policy_source_port = str(30000 + cycle * 100 + case_index)
policy_destination_port = str(40000 + cycle * 100 + case_index)
v4_peer = f"203.0.{cycle}.{10 + case_index}"
v6_peer = f"2001:db8:feed:{cycle}::{case_index}"
local_asn = str(65100 + cycle * 10 + case_index)
remote_asn_v4 = str(65200 + cycle * 10 + case_index)
remote_asn_v6 = str(65300 + cycle * 10 + case_index)

network_doc = {
    "apiVersion": "network.t-caas.telekom.com/v1alpha1",
    "kind": "NodeNetworkConfig",
    "metadata": {
        "name": resource_name,
        "namespace": namespace,
    },
    "spec": {
        "revision": f"{case_name}-rev-{cycle}",
        "localVRFs": {
            vrf_name: {
                "table": table,
                "staticRoutes": [
                    {
                        "prefix": base_v4,
                        "nextHop": {
                            "address": base_v4_next_hop,
                        },
                    },
                    {
                        "prefix": base_v6,
                        "nextHop": {
                            "address": base_v6_next_hop,
                        },
                    },
                ],
            }
        },
    },
}

netplan_doc = {
    "apiVersion": "network.t-caas.telekom.com/v1alpha1",
    "kind": "NodeNetplanConfig",
    "metadata": {
        "name": resource_name,
        "namespace": namespace,
    },
    "spec": {
        "nameservers": [
            base_nameserver,
        ]
    },
}

warning_tokens = []

if case_name == "bgp_v4":
    network_doc["spec"]["localVRFs"][vrf_name]["localASN"] = local_asn
    network_doc["spec"]["localVRFs"][vrf_name]["bgpPeers"] = [
        {
            "address": v4_peer,
            "remoteASN": remote_asn_v4,
            "addressFamilies": ["ipv4-unicast"],
        }
    ]
elif case_name == "bgp_v6_multihop":
    network_doc["spec"]["localVRFs"][vrf_name]["localASN"] = local_asn
    network_doc["spec"]["localVRFs"][vrf_name]["bgpPeers"] = [
        {
            "address": v6_peer,
            "remoteASN": remote_asn_v6,
            "addressFamilies": ["ipv6-unicast"],
            "updateSource": "lo",
            "ebgpMultihop": 2,
        }
    ]
elif case_name == "bgp_dual_stack":
    network_doc["spec"]["localVRFs"][vrf_name]["localASN"] = local_asn
    network_doc["spec"]["localVRFs"][vrf_name]["bgpPeers"] = [
        {
            "address": v4_peer,
            "remoteASN": remote_asn_v4,
            "addressFamilies": ["ipv4-unicast"],
        },
        {
            "address": v6_peer,
            "remoteASN": remote_asn_v6,
            "addressFamilies": ["ipv6-unicast"],
            "updateSource": "lo",
            "ebgpMultihop": 2,
        },
    ]
elif case_name == "policy_udp":
    network_doc["spec"]["localVRFs"][vrf_name]["policyRoutes"] = [
        {
            "trafficMatch": {
                "interface": "eth1",
                "sourcePrefixes": [policy_source],
                "destinationPrefixes": [policy_destination],
                "protocols": ["udp"],
                "sourcePorts": [policy_source_port],
                "destinationPorts": [policy_destination_port],
            },
            "nextHop": {
                "vrf": vrf_name,
            },
        }
    ]
elif case_name == "policy_next_hop_warning":
    network_doc["spec"]["localVRFs"][vrf_name]["policyRoutes"] = [
        {
            "trafficMatch": {
                "interface": "eth1",
                "sourcePrefixes": [policy_source],
                "destinationPrefixes": [policy_destination],
                "protocols": ["udp"],
                "sourcePorts": [policy_source_port],
                "destinationPorts": [policy_destination_port],
            },
            "nextHop": {
                "vrf": vrf_name,
                "address": "192.0.2.254",
            },
        }
    ]
    warning_tokens.append("carries next-hop")
elif case_name == "nameserver_secondary":
    netplan_doc["spec"]["nameservers"] = [base_nameserver, extra_nameserver]
elif case_name == "nameserver_triple":
    netplan_doc["spec"]["nameservers"] = [base_nameserver, extra_nameserver, third_nameserver]
elif case_name == "v4_next_hop":
    network_doc["spec"]["localVRFs"][vrf_name]["staticRoutes"][0]["nextHop"]["address"] = "192.0.2.99"
elif case_name == "v6_next_hop":
    network_doc["spec"]["localVRFs"][vrf_name]["staticRoutes"][1]["nextHop"]["address"] = "2001:db8:ffff::99"
elif case_name == "v4_prefix":
    network_doc["spec"]["localVRFs"][vrf_name]["staticRoutes"][0]["prefix"] = f"198.18.{cycle}.{case_index}/32"
elif case_name == "v6_prefix":
    network_doc["spec"]["localVRFs"][vrf_name]["staticRoutes"][1]["prefix"] = f"2001:db8:beef:{cycle}{case_index:x}::/64"
elif case_name == "table_only":
    pass
elif case_name == "revision_only":
    pass
else:
    raise ValueError(f"unknown case {case_name}")

vrf_tokens = [str(table)]
for route in network_doc["spec"]["localVRFs"][vrf_name]["staticRoutes"]:
    vrf_tokens.append(route["prefix"])
    vrf_tokens.append(route["nextHop"]["address"])

if "localASN" in network_doc["spec"]["localVRFs"][vrf_name]:
    vrf_tokens.append(network_doc["spec"]["localVRFs"][vrf_name]["localASN"])
for peer in network_doc["spec"]["localVRFs"][vrf_name].get("bgpPeers", []):
    vrf_tokens.append(peer["address"])
    vrf_tokens.append(peer["remoteASN"])
    for family in peer.get("addressFamilies", []):
        vrf_tokens.append(family)
    if "updateSource" in peer:
        vrf_tokens.append(peer["updateSource"])
    if "ebgpMultihop" in peer:
        vrf_tokens.append(str(peer["ebgpMultihop"]))

policy_tokens = []
for policy in network_doc["spec"]["localVRFs"][vrf_name].get("policyRoutes", []):
    match = policy["trafficMatch"]
    policy_tokens.extend(
        [
            "eth1",
            match["sourcePrefixes"][0],
            match["destinationPrefixes"][0],
            match["protocols"][0],
            match["sourcePorts"][0],
            match["destinationPorts"][0],
            vrf_name,
        ]
    )

cleanup_static_routes = []
for route in network_doc["spec"]["localVRFs"][vrf_name]["staticRoutes"]:
    family = 6 if ":" in route["prefix"] else 4
    cleanup_static_routes.append(
        {
            "family": family,
            "prefix": route["prefix"],
        }
    )

cleanup_bgp_peers = []
for peer in network_doc["spec"]["localVRFs"][vrf_name].get("bgpPeers", []):
    cleanup_bgp_peers.append(
        {
            "address": peer["address"],
            "families": peer.get("addressFamilies", []),
        }
    )

cleanup_policy_routes = []
for index, policy in enumerate(network_doc["spec"]["localVRFs"][vrf_name].get("policyRoutes", []), start=10):
    family = 6 if any(":" in prefix for prefix in policy["trafficMatch"].get("sourcePrefixes", []) + policy["trafficMatch"].get("destinationPrefixes", [])) else 4
    cleanup_policy_routes.append(
        {
            "family": family,
            "name": f"hbr-{vrf_name}",
            "rule_id": str(index),
            "interface": policy["trafficMatch"].get("interface"),
        }
    )

metadata = {
    "resource_name": resource_name,
    "vrf_name": vrf_name,
    "table": str(table),
    "revision": network_doc["spec"]["revision"],
    "nameservers": netplan_doc["spec"]["nameservers"],
    "cleanup_nameservers": [
        ns for ns in netplan_doc["spec"]["nameservers"] if ns != base_nameserver
    ],
    "vrf_tokens": vrf_tokens,
    "policy_tokens": policy_tokens,
    "warning_tokens": warning_tokens,
    "cleanup_static_routes": cleanup_static_routes,
    "cleanup_bgp_peers": cleanup_bgp_peers,
    "cleanup_policy_routes": cleanup_policy_routes,
    "cleanup_bgp": "localASN" in network_doc["spec"]["localVRFs"][vrf_name],
}

(case_dir / "node-network-config.json").write_text(json.dumps(network_doc, indent=2) + "\n")
(case_dir / "node-netplan-config.json").write_text(json.dumps(netplan_doc, indent=2) + "\n")
(case_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
PY
}

sync_case_files() {
  local cycle="$1"
  local case_name="$2"
  local case_dir="${LOCAL_WORKDIR}/cycle-${cycle}-${case_name}"
  rsync -a "$case_dir/" "${VENUS_HOST}:${REMOTE_WORKDIR}/cycle-${cycle}-${case_name}/"
}

apply_case_files() {
  local cycle="$1"
  local case_name="$2"
  remote_kubectl \
    "apply -f ${REMOTE_WORKDIR}/cycle-${cycle}-${case_name}/node-network-config.json \
     -f ${REMOTE_WORKDIR}/cycle-${cycle}-${case_name}/node-netplan-config.json"
}

delete_case_resources() {
  local cycle="$1"
  local case_name="$2"
  local safe_case_name="${case_name//_/-}"
  local resource_name="vyos-api-matrix-${cycle}-${safe_case_name}"
  remote_kubectl \
    "delete nodenetworkconfig.network.t-caas.telekom.com/${resource_name} \
            nodenetplanconfig.network.t-caas.telekom.com/${resource_name} \
            -n ${TEST_NAMESPACE} --ignore-not-found=true"
}

read_metadata_field() {
  local metadata_file="$1"
  local field="$2"
  "$python_bin" - "$metadata_file" "$field" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text())
value = payload[sys.argv[2]]
if isinstance(value, list):
    for item in value:
        print(item)
else:
    print(value)
PY
}

cleanup_vyos_case() {
  local metadata_file="$1"
  "$python_bin" - "$metadata_file" <<'PY' | while IFS= read -r payload; do
import json
from pathlib import Path
import sys

metadata = json.loads(Path(sys.argv[1]).read_text())
vrf_name = metadata["vrf_name"]

for policy in metadata.get("cleanup_policy_routes", []):
    root = ["policy", "route6" if policy["family"] == 6 else "route", policy["name"]]
    print(json.dumps({"op": "delete", "path": root + ["rule", policy["rule_id"]]}))
    if policy.get("interface"):
        print(json.dumps({"op": "delete", "path": root + ["interface"]}))
    print(json.dumps({"op": "delete", "path": root}))

for route in metadata.get("cleanup_static_routes", []):
    root = ["vrf", "name", vrf_name, "protocols", "static", "route6" if route["family"] == 6 else "route", route["prefix"]]
    print(json.dumps({"op": "delete", "path": root}))

if metadata.get("cleanup_bgp"):
    for peer in metadata.get("cleanup_bgp_peers", []):
        print(json.dumps({"op": "delete", "path": ["vrf", "name", vrf_name, "protocols", "bgp", "neighbor", peer["address"]]}))
    print(json.dumps({"op": "delete", "path": ["vrf", "name", vrf_name, "protocols", "bgp", "system-as"]}))

print(json.dumps({"op": "delete", "path": ["vrf", "name", vrf_name, "table"]}))
print(json.dumps({"op": "delete", "path": ["vrf", "name", vrf_name]}))

for nameserver in metadata.get("cleanup_nameservers", []):
    print(json.dumps({"op": "delete", "path": ["system", "name-server", nameserver]}))
PY
    [[ -z "$payload" ]] && continue
    curl -sk --connect-timeout 5 \
      --form "data=${payload}" \
      --form "key=${VYOS_API_KEY}" \
      "${VYOS_URL%/}/configure" >/dev/null || true
  done
  api_save
}

run_controller_json() {
  local out_file="$1"
  shift
  "$python_bin" -m hbr_vyos_adapter.cli \
    controller "$@" --json >"$out_file"
}

verify_controller_apply() {
  local out_file="$1"
  local metadata_file="$2"
  local expect_apply="$3"
  "$python_bin" - "$out_file" "$metadata_file" "$expect_apply" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text())
metadata = json.loads(Path(sys.argv[2]).read_text())
expect_apply = sys.argv[3] == "true"
iteration = payload["iterations"][0]
assert iteration["ok"] is True, iteration
assert iteration["status_write_performed"] is True, iteration
docs = {item["kind"]: item for item in iteration["reconcile"]["documents"]}
network = docs["NodeNetworkConfig"]
netplan = docs["NodeNetplanConfig"]
assert network["desired_revision"] == metadata["revision"], network
assert network["in_sync"] is True, network
assert netplan["in_sync"] is True, netplan
joined_warnings = "\n".join(network["warnings"])
for token in metadata["warning_tokens"]:
    assert token in joined_warnings, (token, joined_warnings)
if expect_apply:
    assert iteration["apply_performed"] is True, iteration
    assert iteration["pending_command_count"] > 0, iteration
    assert iteration["reconcile"]["vyos_response"]["success"] is True, iteration
else:
    assert iteration["apply_performed"] is False, iteration
    assert iteration["pending_command_count"] == 0, iteration
    assert network["action"] == "noop", network
    assert netplan["action"] == "noop", netplan
PY
}

verify_controller_deleted() {
  local out_file="$1"
  "$python_bin" - "$out_file" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text())
iteration = payload["iterations"][0]
assert iteration["ok"] is True, iteration
assert iteration["deleted_documents"] == 2, iteration
assert iteration["status_patch_count"] == 0, iteration
assert iteration["status_write"]["skipped"], iteration
PY
}

verify_cluster_status() {
  local cycle="$1"
  local case_name="$2"
  local out_file="$3"
  local safe_case_name="${case_name//_/-}"
  local resource_name="vyos-api-matrix-${cycle}-${safe_case_name}"
  remote_run \
    "${REMOTE_KUBECTL} --kubeconfig ${REMOTE_KUBECONFIG} get \
     nodenetworkconfig.network.t-caas.telekom.com/${resource_name} \
     nodenetplanconfig.network.t-caas.telekom.com/${resource_name} \
     -n ${TEST_NAMESPACE} -o json" >"$out_file"
  "$python_bin" - "$out_file" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text())
items = {item["kind"]: item for item in payload["items"]}
assert items["NodeNetworkConfig"]["status"]["phase"] == "InSync", items
assert items["NodeNetworkConfig"]["status"]["appliedRevision"], items
assert items["NodeNetplanConfig"]["status"]["phase"] == "InSync", items
assert items["NodeNetplanConfig"]["status"]["appliedRevision"], items
PY
}

verify_vyos_case() {
  local metadata_file="$1"
  local vrf_out="$2"
  local policy_out="$3"
  local nameserver_out="$4"
  local vrf_name
  vrf_name="$(read_metadata_field "$metadata_file" vrf_name | head -n1)"
  curl -sk --connect-timeout 5 \
    --form "data={\"op\":\"showConfig\",\"path\":[\"vrf\",\"name\",\"${vrf_name}\"]}" \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >"$vrf_out"
  curl -sk --connect-timeout 5 \
    --form "data={\"op\":\"showConfig\",\"path\":[\"policy\",\"route\",\"hbr-${vrf_name}\"]}" \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >"$policy_out"
  curl -sk --connect-timeout 5 \
    --form 'data={"op":"showConfig","path":["system","name-server"]}' \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >"$nameserver_out"
  "$python_bin" - "$metadata_file" "$vrf_out" "$policy_out" "$nameserver_out" <<'PY'
import json
from pathlib import Path
import sys

metadata = json.loads(Path(sys.argv[1]).read_text())
vrf_payload = json.loads(Path(sys.argv[2]).read_text())
policy_payload = json.loads(Path(sys.argv[3]).read_text())
nameserver_payload = json.loads(Path(sys.argv[4]).read_text())

assert vrf_payload["success"] is True, vrf_payload
vrf_text = json.dumps(vrf_payload["data"], sort_keys=True)
for token in metadata["vrf_tokens"]:
    assert token in vrf_text, (token, vrf_text)

policy_tokens = metadata["policy_tokens"]
if policy_tokens:
    assert policy_payload["success"] is True, policy_payload
    policy_text = json.dumps(policy_payload["data"], sort_keys=True)
    for token in policy_tokens:
        assert token in policy_text, (token, policy_text)
else:
    if policy_payload["success"]:
        policy_text = json.dumps(policy_payload["data"], sort_keys=True)
        assert "rule" not in policy_text, policy_text

assert nameserver_payload["success"] is True, nameserver_payload
nameserver_text = json.dumps(nameserver_payload["data"], sort_keys=True)
for token in metadata["nameservers"]:
    assert token in nameserver_text, (token, nameserver_text)
PY
}

verify_vyos_cleanup() {
  local metadata_file="$1"
  local vrf_out="$2"
  local policy_out="$3"
  local nameserver_out="$4"
  local vrf_name
  vrf_name="$(read_metadata_field "$metadata_file" vrf_name | head -n1)"
  curl -sk --connect-timeout 5 \
    --form "data={\"op\":\"showConfig\",\"path\":[\"vrf\",\"name\",\"${vrf_name}\"]}" \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >"$vrf_out"
  curl -sk --connect-timeout 5 \
    --form "data={\"op\":\"showConfig\",\"path\":[\"policy\",\"route\",\"hbr-${vrf_name}\"]}" \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >"$policy_out"
  curl -sk --connect-timeout 5 \
    --form 'data={"op":"showConfig","path":["system","name-server"]}' \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >"$nameserver_out"
  "$python_bin" - "$metadata_file" "$vrf_out" "$policy_out" "$nameserver_out" "$BASE_NAMESERVER" <<'PY'
import json
from pathlib import Path
import sys

metadata = json.loads(Path(sys.argv[1]).read_text())
vrf_payload = json.loads(Path(sys.argv[2]).read_text())
policy_payload = json.loads(Path(sys.argv[3]).read_text())
nameserver_payload = json.loads(Path(sys.argv[4]).read_text())
base_nameserver = sys.argv[5]

assert vrf_payload["success"] is False, vrf_payload
if policy_payload["success"]:
    policy_text = json.dumps(policy_payload["data"], sort_keys=True)
    assert "rule" not in policy_text, policy_text
nameserver_text = json.dumps(nameserver_payload["data"], sort_keys=True)
assert base_nameserver in nameserver_text, nameserver_text
for token in metadata["cleanup_nameservers"]:
    assert token not in nameserver_text, (token, nameserver_text)
PY
}

run_case() {
  local cycle="$1"
  local case_index="$2"
  local case_name="$3"
  local case_root="${ARTIFACT_DIR}/cycle-${cycle}/${case_name}"
  local state_file="${LOCAL_WORKDIR}/cycle-${cycle}-${case_name}-state.json"
  local status_file="${LOCAL_WORKDIR}/cycle-${cycle}-${case_name}-status.json"
  local apply_out="${case_root}/controller-apply.json"
  local noop_out="${case_root}/controller-noop.json"
  local delete_out="${case_root}/controller-delete.json"
  local cluster_out="${case_root}/cluster-status.json"
  local vrf_out="${case_root}/vyos-vrf.json"
  local policy_out="${case_root}/vyos-policy.json"
  local nameserver_out="${case_root}/vyos-nameserver.json"
  local cleanup_vrf_out="${case_root}/cleanup-vrf.json"
  local cleanup_policy_out="${case_root}/cleanup-policy.json"
  local cleanup_nameserver_out="${case_root}/cleanup-nameserver.json"
  local metadata_file="${LOCAL_WORKDIR}/cycle-${cycle}-${case_name}/metadata.json"

  mkdir -p "$case_root"
  rm -f "$state_file" "$status_file"

  log "Cycle ${cycle}: case ${case_name}"
  delete_case_resources "$cycle" "$case_name"
  generate_case_files "$cycle" "$case_index" "$case_name"
  sync_case_files "$cycle" "$case_name"
  apply_case_files "$cycle" "$case_name"

  run_controller_json "$apply_out" \
    --source kubernetes \
    --kubeconfig "$LOCAL_KUBECONFIG" \
    --source-namespace "$TEST_NAMESPACE" \
    --resource-kind NodeNetworkConfig \
    --resource-kind NodeNetplanConfig \
    --state-file "$state_file" \
    --status-file "$status_file" \
    --once \
    --apply \
    --vyos-url "$VYOS_URL" \
    --api-key "$VYOS_API_KEY" \
    --vyos-timeout "$VYOS_TIMEOUT" \
    --write-status
  verify_controller_apply "$apply_out" "$metadata_file" "true"
  verify_cluster_status "$cycle" "$case_name" "$cluster_out"
  verify_vyos_case "$metadata_file" "$vrf_out" "$policy_out" "$nameserver_out"

  run_controller_json "$noop_out" \
    --source kubernetes \
    --kubeconfig "$LOCAL_KUBECONFIG" \
    --source-namespace "$TEST_NAMESPACE" \
    --resource-kind NodeNetworkConfig \
    --resource-kind NodeNetplanConfig \
    --state-file "$state_file" \
    --status-file "$status_file" \
    --once \
    --apply \
    --vyos-url "$VYOS_URL" \
    --api-key "$VYOS_API_KEY" \
    --vyos-timeout "$VYOS_TIMEOUT" \
    --write-status
  verify_controller_apply "$noop_out" "$metadata_file" "false"

  delete_case_resources "$cycle" "$case_name"
  run_controller_json "$delete_out" \
    --source kubernetes \
    --kubeconfig "$LOCAL_KUBECONFIG" \
    --source-namespace "$TEST_NAMESPACE" \
    --resource-kind NodeNetworkConfig \
    --resource-kind NodeNetplanConfig \
    --state-file "$state_file" \
    --status-file "$status_file" \
    --once \
    --write-status \
    --dry-run-status
  verify_controller_deleted "$delete_out"

  cleanup_vyos_case "$metadata_file"
  verify_vyos_cleanup "$metadata_file" "$cleanup_vrf_out" "$cleanup_policy_out" "$cleanup_nameserver_out"
}

main() {
  local phase="${1:-}"
  if [[ "$phase" != "run" ]]; then
    usage
    exit 1
  fi

  mkdir -p "$LOCAL_WORKDIR" "$ARTIFACT_DIR"
  check_venus_access
  sync_adapter
  prepare_cluster
  prepare_local_kubeconfig
  start_tunnel

  log "Checking VyOS API reachability"
  curl -ksS --connect-timeout 5 \
    --form 'data={"op":"showConfig","path":[]}' \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >/dev/null

  local cases=(
    revision_only
    table_only
    v4_prefix
    v4_next_hop
    v6_prefix
    v6_next_hop
    bgp_v4
    bgp_v6_multihop
    bgp_dual_stack
    policy_udp
    policy_next_hop_warning
    nameserver_secondary
    nameserver_triple
  )

  local cycle case_index case_name
  for ((cycle = 1; cycle <= CYCLES; cycle++)); do
    case_index=0
    for case_name in "${cases[@]}"; do
      case_index=$((case_index + 1))
      run_case "$cycle" "$case_index" "$case_name"
    done
  done

  api_save
  log "All ${CYCLES} API matrix cycles passed"
  log "Artifacts saved under ${ARTIFACT_DIR}"
}

main "$@"
