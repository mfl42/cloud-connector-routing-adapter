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
ITERATIONS="${ITERATIONS:-3}"
TUNNEL_PORT="${TUNNEL_PORT:-16443}"
TEST_NAMESPACE="${TEST_NAMESPACE:-hbr-smoke}"
LOCAL_WORKDIR="${LOCAL_WORKDIR:-/tmp/hbr-deeper-smoke}"
REMOTE_WORKDIR="${REMOTE_WORKDIR:-/tmp/hbr-deeper-smoke}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$repo_root/artifacts/private/hbr-deeper-smoke}"
LOCAL_KUBECONFIG="${LOCAL_WORKDIR}/venus-rke2.yaml"

tunnel_pid=""

usage() {
  cat <<'EOF'
Usage:
  scripts/deeper-smoke-vyos-vm.sh run

Environment:
  ITERATIONS        Number of full baseline/mutate/delete cycles to run (default: 3)
  TEST_NAMESPACE    Dedicated Kubernetes namespace for the deeper test (default: hbr-smoke)
  VENUS_HOST        SSH target for the lab host (default: user@venus.example.test)
  REMOTE_REPO       repo path on the lab host (default: /home/user/cloud-connector-routing-adapter)
  REMOTE_KUBECTL    kubectl path on Venus (default: /var/lib/rancher/rke2/bin/kubectl)
  REMOTE_KUBECONFIG RKE2 kubeconfig path on Venus (default: /etc/rancher/rke2/rke2.yaml)
  VYOS_URL          VyOS API URL (default: https://vyos.example.test)
  VYOS_API_KEY      VyOS API key (default: replace-with-real-api-key)
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
  log "Installing lab CRDs on the Venus RKE2 cluster"
  remote_run "mkdir -p ${REMOTE_WORKDIR}"
  remote_kubectl "create namespace ${TEST_NAMESPACE} --dry-run=client -o yaml | ${REMOTE_KUBECTL} --kubeconfig ${REMOTE_KUBECONFIG} apply -f -"
  remote_kubectl \
    "apply -f ${REMOTE_REPO}/k8s/crds/node-network-config-crd.yaml \
     -f ${REMOTE_REPO}/k8s/crds/node-netplan-config-crd.yaml"
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

generate_iteration_files() {
  local iter="$1"
  local mode="$2"
  local iter_dir="${LOCAL_WORKDIR}/iter-${iter}-${mode}"
  mkdir -p "$iter_dir"
  "$python_bin" - "$iter_dir" "$iter" "$mode" "$TEST_NAMESPACE" <<'PY'
import json
from pathlib import Path
import sys

iter_dir = Path(sys.argv[1])
iteration = int(sys.argv[2])
mode = sys.argv[3]
namespace = sys.argv[4]

resource_name = f"vyos-lab-deep-{iteration}"
vrf_name = f"smoke-{iteration}"
table_id = 1100 + iteration
base_v4 = f"198.51.{100 + iteration}.0/24"
base_v6 = f"2001:db8:{iteration}::/64"
extra_v4 = f"203.0.{iteration}.0/24"

network_doc = {
    "apiVersion": "network.t-caas.telekom.com/v1alpha1",
    "kind": "NodeNetworkConfig",
    "metadata": {
        "name": resource_name,
        "namespace": namespace,
    },
    "spec": {
        "revision": f"deep-{iteration}-{'mutate' if mode == 'mutate' else 'base'}",
        "localVRFs": {
            vrf_name: {
                "table": table_id,
                "staticRoutes": [
                    {
                        "prefix": base_v4,
                        "nextHop": {
                            "address": "192.0.2.1",
                        },
                    },
                    {
                        "prefix": base_v6,
                        "nextHop": {
                            "address": "2001:db8::1",
                        },
                    },
                ],
            }
        },
    },
}

if mode == "mutate":
    network_doc["spec"]["localVRFs"][vrf_name]["staticRoutes"].append(
        {
            "prefix": extra_v4,
            "nextHop": {
                "address": "192.0.2.254",
            },
        }
    )

netplan_doc = {
    "apiVersion": "network.t-caas.telekom.com/v1alpha1",
    "kind": "NodeNetplanConfig",
    "metadata": {
        "name": resource_name,
        "namespace": namespace,
    },
    "spec": {
        "nameservers": [
            "192.0.2.53",
        ]
    },
}

(iter_dir / "node-network-config.json").write_text(json.dumps(network_doc, indent=2) + "\n")
(iter_dir / "node-netplan-config.json").write_text(json.dumps(netplan_doc, indent=2) + "\n")
PY
}

sync_iteration_files() {
  local iter="$1"
  local mode="$2"
  local iter_dir="${LOCAL_WORKDIR}/iter-${iter}-${mode}"
  rsync -a "$iter_dir/" "${VENUS_HOST}:${REMOTE_WORKDIR}/iter-${iter}-${mode}/"
}

apply_iteration_files() {
  local iter="$1"
  local mode="$2"
  remote_kubectl \
    "apply -f ${REMOTE_WORKDIR}/iter-${iter}-${mode}/node-network-config.json \
     -f ${REMOTE_WORKDIR}/iter-${iter}-${mode}/node-netplan-config.json"
}

delete_iteration_resources() {
  local iter="$1"
  local resource_name="vyos-lab-deep-${iter}"
  remote_kubectl \
    "delete nodenetworkconfig.network.t-caas.telekom.com/${resource_name} \
            nodenetplanconfig.network.t-caas.telekom.com/${resource_name} \
            -n ${TEST_NAMESPACE} --ignore-not-found=true"
}

cleanup_vyos_vrf() {
  local iter="$1"
  local vrf_name="smoke-${iter}"
  curl -sk --connect-timeout 5 \
    --form "data={\"op\":\"delete\",\"path\":[\"vrf\",\"name\",\"${vrf_name}\"]}" \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/configure" >/dev/null || true
  api_save
}

verify_vyos_vrf_present() {
  local iter="$1"
  local mode="$2"
  local out_file="$3"
  local vrf_name="smoke-${iter}"
  curl -sk --connect-timeout 5 \
    --form "data={\"op\":\"showConfig\",\"path\":[\"vrf\",\"name\",\"${vrf_name}\"]}" \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >"$out_file"
  "$python_bin" - "$out_file" "$iter" "$mode" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text())
iteration = int(sys.argv[2])
mode = sys.argv[3]
table_id = str(1100 + iteration)
base_v4 = f"198.51.{100 + iteration}.0/24"
base_v6 = f"2001:db8:{iteration}::/64"
extra_v4 = f"203.0.{iteration}.0/24"

assert payload["success"] is True, payload
data = payload["data"]
assert data["table"] == table_id, data
route_block = data["protocols"]["static"]
assert base_v4 in route_block["route"], route_block
assert base_v6 in route_block["route6"], route_block
if mode == "mutate":
    assert extra_v4 in route_block["route"], route_block
else:
    assert extra_v4 not in route_block["route"], route_block
PY
}

verify_vyos_vrf_absent() {
  local iter="$1"
  local out_file="$2"
  local vrf_name="smoke-${iter}"
  curl -sk --connect-timeout 5 \
    --form "data={\"op\":\"showConfig\",\"path\":[\"vrf\",\"name\",\"${vrf_name}\"]}" \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >"$out_file"
  "$python_bin" - "$out_file" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text())
assert payload["success"] is False, payload
PY
}

run_controller_json() {
  local out_file="$1"
  shift
  "$python_bin" -m hbr_vyos_adapter.cli \
    controller "$@" --json >"$out_file"
}

verify_controller_apply() {
  local out_file="$1"
  local expected_revision="$2"
  local expected_apply="$3"
  "$python_bin" - "$out_file" "$expected_revision" "$expected_apply" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text())
expected_revision = sys.argv[2]
expected_apply = sys.argv[3] == "true"
iteration = payload["iterations"][0]
assert iteration["ok"] is True, iteration
assert iteration["status_write_performed"] is True, iteration
assert iteration["reconcile"]["documents"], iteration

docs = {item["kind"]: item for item in iteration["reconcile"]["documents"]}
network = docs["NodeNetworkConfig"]
netplan = docs["NodeNetplanConfig"]
assert network["desired_revision"] == expected_revision, network
assert network["in_sync"] is True, network
assert netplan["in_sync"] is True, netplan

if expected_apply:
    assert iteration["apply_performed"] is True, iteration
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
  local iter="$1"
  local out_file="$2"
  local resource_name="vyos-lab-deep-${iter}"
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

run_iteration() {
  local iter="$1"
  local iter_artifacts="${ARTIFACT_DIR}/iteration-${iter}"
  local state_file="${LOCAL_WORKDIR}/iteration-${iter}-state.json"
  local status_file="${LOCAL_WORKDIR}/iteration-${iter}-status.json"
  local base_out="${iter_artifacts}/controller-base.json"
  local noop_out="${iter_artifacts}/controller-noop.json"
  local mutate_out="${iter_artifacts}/controller-mutate.json"
  local delete_out="${iter_artifacts}/controller-delete.json"
  local cluster_out="${iter_artifacts}/cluster-status.json"
  local vyos_base_out="${iter_artifacts}/vyos-base.json"
  local vyos_mutate_out="${iter_artifacts}/vyos-mutate.json"
  local vyos_delete_out="${iter_artifacts}/vyos-delete.json"

  mkdir -p "$iter_artifacts"

  log "Iteration ${iter}: baseline apply"
  delete_iteration_resources "$iter"
  cleanup_vyos_vrf "$iter"
  generate_iteration_files "$iter" "base"
  sync_iteration_files "$iter" "base"
  apply_iteration_files "$iter" "base"
  run_controller_json "$base_out" \
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
    --write-status
  verify_controller_apply "$base_out" "deep-${iter}-base" "true"
  verify_cluster_status "$iter" "$cluster_out"
  verify_vyos_vrf_present "$iter" "base" "$vyos_base_out"

  log "Iteration ${iter}: idempotency re-run"
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
    --write-status
  verify_controller_apply "$noop_out" "deep-${iter}-base" "false"

  log "Iteration ${iter}: additive mutation"
  generate_iteration_files "$iter" "mutate"
  sync_iteration_files "$iter" "mutate"
  apply_iteration_files "$iter" "mutate"
  run_controller_json "$mutate_out" \
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
    --write-status
  verify_controller_apply "$mutate_out" "deep-${iter}-mutate" "true"
  verify_cluster_status "$iter" "$cluster_out"
  verify_vyos_vrf_present "$iter" "mutate" "$vyos_mutate_out"

  log "Iteration ${iter}: Kubernetes deletion tombstone path"
  delete_iteration_resources "$iter"
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
  cleanup_vyos_vrf "$iter"
  verify_vyos_vrf_absent "$iter" "$vyos_delete_out"
}

main() {
  local phase="${1:-}"
  if [[ "$phase" != "run" ]]; then
    usage
    exit 1
  fi

  mkdir -p "$LOCAL_WORKDIR" "$ARTIFACT_DIR"
  sync_adapter
  prepare_cluster
  prepare_local_kubeconfig
  start_tunnel

  log "Checking VyOS API reachability"
  curl -ksS --connect-timeout 5 \
    --form 'data={"op":"showConfig","path":[]}' \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >/dev/null

  for ((iter = 1; iter <= ITERATIONS; iter++)); do
    run_iteration "$iter"
  done

  api_save
  log "All ${ITERATIONS} deeper smoke iterations passed"
  log "Artifacts saved under ${ARTIFACT_DIR}"
}

main "$@"
