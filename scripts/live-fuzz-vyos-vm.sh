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
TUNNEL_PORT="${TUNNEL_PORT:-17443}"
TEST_NAMESPACE="${TEST_NAMESPACE:-hbr-live-fuzz}"
SEEDS="${SEEDS:-2}"
CASES_PER_SEED="${CASES_PER_SEED:-10}"
LOCAL_WORKDIR="${LOCAL_WORKDIR:-/tmp/hbr-live-fuzz}"
REMOTE_WORKDIR="${REMOTE_WORKDIR:-/tmp/hbr-live-fuzz}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$repo_root/artifacts/private/hbr-live-fuzz}"
LOCAL_KUBECONFIG="${LOCAL_WORKDIR}/venus-rke2.yaml"
BASE_NAMESERVER="${BASE_NAMESERVER:-192.0.2.53}"
MGMT_ADDRESS="${MGMT_ADDRESS:-192.0.2.230/24}"
MGMT_DEFAULT_ROUTE="${MGMT_DEFAULT_ROUTE:-192.0.2.1}"

tunnel_pid=""

usage() {
  cat <<'EOF'
Usage:
  scripts/live-fuzz-vyos-vm.sh run

Environment:
  SEEDS             Number of deterministic fuzz seeds to run (default: 2)
  CASES_PER_SEED    Number of live randomized cases per seed (default: 10)
  TEST_NAMESPACE    Kubernetes namespace used for live fuzzing (default: hbr-live-fuzz)
  VENUS_HOST        SSH target for the lab host (default: user@venus.example.test)
  REMOTE_REPO       repo path on the lab host (default: /home/user/cloud-connector-routing-adapter)
  VYOS_URL          VyOS API URL (default: https://vyos.example.test)
  VYOS_API_KEY      VyOS API key (default: replace-with-real-api-key)
  VYOS_TIMEOUT      VyOS API timeout in seconds (default: 90)
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

check_venus_access() {
  log "Checking SSH access to Venus"
  ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
    "$VENUS_HOST" "printf 'venus-ok\n'" >/dev/null
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
  log "Preparing the Venus RKE2 cluster for live fuzzing"
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

check_vyos_api() {
  log "Checking VyOS API reachability"
  curl -sk --connect-timeout 5 \
    --form 'data={"op":"showConfig","path":[]}' \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >/dev/null
}

api_save() {
  curl -sk --connect-timeout 5 \
    --form 'data={"op":"save"}' \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/config-file" >/dev/null
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
  local vrf_name
  vrf_name="$(read_metadata_field "$metadata_file" vrf_name | head -n1)"
  curl -sk --connect-timeout 5 \
    --form "data={\"op\":\"delete\",\"path\":[\"policy\",\"route\",\"hbr-${vrf_name}\"]}" \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/configure" >/dev/null || true
  curl -sk --connect-timeout 5 \
    --form "data={\"op\":\"delete\",\"path\":[\"policy\",\"route6\",\"hbr-${vrf_name}\"]}" \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/configure" >/dev/null || true
  curl -sk --connect-timeout 5 \
    --form "data={\"op\":\"delete\",\"path\":[\"vrf\",\"name\",\"${vrf_name}\"]}" \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/configure" >/dev/null || true
  while IFS= read -r nameserver; do
    [[ -z "$nameserver" ]] && continue
    curl -sk --connect-timeout 5 \
      --form "data={\"op\":\"delete\",\"path\":[\"system\",\"name-server\",\"${nameserver}\"]}" \
      --form "key=${VYOS_API_KEY}" \
      "${VYOS_URL%/}/configure" >/dev/null || true
  done < <(read_metadata_field "$metadata_file" cleanup_nameservers || true)
  api_save
}

generate_case_files() {
  local seed="$1"
  local case_index="$2"
  local case_dir="$3"
  mkdir -p "$case_dir"
  "$python_bin" - "$seed" "$case_index" "$case_dir" "$TEST_NAMESPACE" "$BASE_NAMESERVER" "$MGMT_ADDRESS" "$MGMT_DEFAULT_ROUTE" <<'PY'
import ipaddress
import json
import random
from pathlib import Path
import sys

seed = int(sys.argv[1])
case_index = int(sys.argv[2])
case_dir = Path(sys.argv[3])
namespace = sys.argv[4]
base_nameserver = sys.argv[5]
mgmt_address = sys.argv[6]
mgmt_default_route = sys.argv[7]
rng = random.Random(seed * 1000 + case_index)

name = f"vyos-live-fuzz-{seed}-{case_index}"
vrf_name = f"fuzz-{seed}-{case_index}"
table_id = 20000 + (seed * 100) + case_index

def ipv4_host():
    first_octet = rng.choice([*range(11, 127), *range(128, 224)])
    return ".".join([str(first_octet)] + [str(rng.randint(1, 254)) for _ in range(3)])

def ipv6_host():
    return ":".join(f"{rng.randint(0, 0xFFFF):x}" for _ in range(8))

def prefix(family):
    if family == 6:
        prefix_length = rng.choice([48, 56, 64, 80])
        raw = ipaddress.IPv6Address(rng.getrandbits(128))
        network = ipaddress.IPv6Network((raw, prefix_length), strict=False)
        return network.with_prefixlen
    octets = [rng.randint(1, 223), rng.randint(0, 255), rng.randint(0, 255)]
    if octets[0] == 127:
        octets[0] = 126
    return f"{octets[0]}.{octets[1]}.{octets[2]}.0/{rng.choice([24, 25, 26, 27, 28, 32])}"

# Keep live fuzzing away from management and dataplane interface attachment.
# Interface-to-VRF moves are useful in local fuzzing, but too disruptive here.
interfaces = []

bgp_peers = []

policy_routes = []
for _ in range(rng.randint(1, 2)):
    family = 4
    protocol = rng.choice(["tcp", "udp"])
    entry = {
        "trafficMatch": {
            "interface": None,
            "sourcePrefixes": [prefix(family)],
            "destinationPrefixes": [prefix(family)],
            "protocols": [protocol],
        },
        "nextHop": {
            "vrf": vrf_name,
        },
    }
    if protocol in {"tcp", "udp"} and rng.random() < 0.5:
        entry["trafficMatch"]["sourcePorts"] = [str(rng.randint(1024, 32767))]
    if protocol in {"tcp", "udp"} and rng.random() < 0.5:
        entry["trafficMatch"]["destinationPorts"] = [str(rng.randint(1024, 32767))]
    policy_routes.append(entry)

static_routes = []
for _ in range(rng.randint(1, 3)):
    family = 6 if rng.random() < 0.35 else 4
    entry = {
        "prefix": prefix(family),
        "nextHop": {
            "address": ipv6_host() if family == 6 else ipv4_host(),
        },
    }
    if rng.random() < 0.15:
        entry["prefix"] = ""
    static_routes.append(entry)

nameserver_pool = [
    base_nameserver,
    "1.1.1.1",
    "8.8.8.8",
    "9.9.9.9",
]
rng.shuffle(nameserver_pool)

network_doc = {
    "apiVersion": "network.t-caas.telekom.com/v1alpha1",
    "kind": "NodeNetworkConfig",
    "metadata": {
        "name": name,
        "namespace": namespace,
        "generation": case_index,
        "resourceVersion": str(case_index),
    },
    "spec": {
        "revision": f"live-fuzz-{seed}-{case_index}",
        "localVRFs": {
            vrf_name: {
                "localASN": rng.choice([65001, 65010, 65040, 65100]),
                "table": table_id,
                "interfaces": interfaces,
                "bgpPeers": bgp_peers,
                "policyRoutes": policy_routes,
                "staticRoutes": static_routes,
            }
        },
    },
}

netplan_doc = {
    "apiVersion": "network.t-caas.telekom.com/v1alpha1",
    "kind": "NodeNetplanConfig",
    "metadata": {
        "name": name,
        "namespace": namespace,
        "generation": case_index,
        "resourceVersion": f"net-{case_index}",
    },
    "spec": {
        "interfaces": {
            "eth1": {
                "addresses": [mgmt_address],
                "routes": [
                    {
                        "to": "0.0.0.0/0",
                        "via": mgmt_default_route,
                    }
                ],
            }
        },
        "nameservers": nameserver_pool[: rng.randint(1, 3)],
    },
}

(case_dir / "node-network-config.json").write_text(json.dumps(network_doc, indent=2) + "\n")
(case_dir / "node-netplan-config.json").write_text(json.dumps(netplan_doc, indent=2) + "\n")

metadata = {
    "name": name,
    "vrf_name": vrf_name,
    "cleanup_nameservers": [ns for ns in netplan_doc["spec"]["nameservers"] if ns != base_nameserver],
}
(case_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
PY
}

run_case() {
  local seed="$1"
  local case_index="$2"
  local case_dir="${ARTIFACT_DIR}/seed-${seed}/case-${case_index}"
  local remote_case_dir="${REMOTE_WORKDIR}/seed-${seed}-case-${case_index}"
  local metadata_file="${case_dir}/metadata.json"

  log "Live fuzz seed ${seed} case ${case_index}"
  rm -rf "$case_dir"
  generate_case_files "$seed" "$case_index" "$case_dir"
  cleanup_vyos_case "$metadata_file"

  remote_run "mkdir -p ${remote_case_dir}"
  rsync -a "${case_dir}/" "${VENUS_HOST}:${remote_case_dir}/"

  remote_kubectl \
    "apply -f ${remote_case_dir}/node-network-config.json -f ${remote_case_dir}/node-netplan-config.json"

  "$python_bin" -m hbr_vyos_adapter.cli \
    controller \
    --source kubernetes \
    --kubeconfig "$LOCAL_KUBECONFIG" \
    --source-namespace "$TEST_NAMESPACE" \
    --state-file "${case_dir}/controller-state.json" \
    --status-file "${case_dir}/controller-status.json" \
    --once \
    --write-status \
    --dry-run-status \
    --apply \
    --vyos-url "$VYOS_URL" \
    --api-key "$VYOS_API_KEY" \
    --vyos-timeout "$VYOS_TIMEOUT" \
    --json \
    >"${case_dir}/controller-first.json"

  "$python_bin" -m hbr_vyos_adapter.cli \
    controller \
    --source kubernetes \
    --kubeconfig "$LOCAL_KUBECONFIG" \
    --source-namespace "$TEST_NAMESPACE" \
    --state-file "${case_dir}/controller-state.json" \
    --status-file "${case_dir}/controller-status.json" \
    --once \
    --write-status \
    --dry-run-status \
    --apply \
    --vyos-url "$VYOS_URL" \
    --api-key "$VYOS_API_KEY" \
    --vyos-timeout "$VYOS_TIMEOUT" \
    --json \
    >"${case_dir}/controller-second.json"

  "$python_bin" - "${case_dir}/controller-first.json" "${case_dir}/controller-second.json" "${case_dir}/summary.json" "$seed" "$case_index" <<'PY'
import json
import sys
from pathlib import Path

first = json.loads(Path(sys.argv[1]).read_text())
second = json.loads(Path(sys.argv[2]).read_text())
seed = int(sys.argv[4])
case_index = int(sys.argv[5])

assert len(first["iterations"]) == 1, first
assert len(second["iterations"]) == 1, second

first_iteration = first["iterations"][0]
second_iteration = second["iterations"][0]

assert first_iteration["ok"] is True, first_iteration
assert second_iteration["ok"] is True, second_iteration
assert second_iteration["pending_command_count"] == 0, second_iteration
assert second_iteration["apply_performed"] is False, second_iteration

summary = {
    "seed": seed,
    "case": case_index,
    "first_changed_documents": first_iteration["changed_documents"],
    "first_pending_command_count": first_iteration["pending_command_count"],
    "first_apply_performed": first_iteration["apply_performed"],
    "second_pending_command_count": second_iteration["pending_command_count"],
    "second_apply_performed": second_iteration["apply_performed"],
}

Path(sys.argv[3]).write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary))
PY

  remote_kubectl \
    "delete -f ${remote_case_dir}/node-network-config.json -f ${remote_case_dir}/node-netplan-config.json --ignore-not-found=true"
  cleanup_vyos_case "$metadata_file"
}

write_summary() {
  "$python_bin" - "$ARTIFACT_DIR" "$SEEDS" "$CASES_PER_SEED" <<'PY'
import json
from pathlib import Path
import sys

artifact_dir = Path(sys.argv[1])
seed_count = int(sys.argv[2])
cases_per_seed = int(sys.argv[3])
summaries = []

for seed in range(1, seed_count + 1):
    for case_index in range(1, cases_per_seed + 1):
        path = artifact_dir / f"seed-{seed}" / f"case-{case_index}" / "summary.json"
        if not path.exists():
            raise SystemExit(f"missing live fuzz summary: {path}")
        summaries.append(json.loads(path.read_text()))

payload = {
    "seedCount": seed_count,
    "casesPerSeed": cases_per_seed,
    "caseCount": len(summaries),
    "artifactDir": str(artifact_dir),
    "cases": summaries,
}
(artifact_dir / "summary.json").write_text(json.dumps(payload, indent=2) + "\n")
print(json.dumps(payload, indent=2))
PY
}

main() {
  if [[ "${1:-}" != "run" ]]; then
    usage
    exit 1
  fi

  check_venus_access
  sync_adapter
  prepare_cluster
  prepare_local_kubeconfig
  start_tunnel
  check_vyos_api

  mkdir -p "$ARTIFACT_DIR"
  rm -f "$ARTIFACT_DIR/summary.json"

  local seed
  local case_index
  for ((seed=1; seed<=SEEDS; seed++)); do
    for ((case_index=1; case_index<=CASES_PER_SEED; case_index++)); do
      run_case "$seed" "$case_index"
    done
  done

  api_save
  write_summary

  log "Live fuzzing completed successfully"
  log "Artifacts saved under ${ARTIFACT_DIR}"
}

main "$@"
