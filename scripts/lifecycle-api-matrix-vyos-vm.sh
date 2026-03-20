#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

VENUS_HOST="${VENUS_HOST:-user@venus.example.test}"
VM_NAME="${VM_NAME:-vyos-vpp-lab}"
REMOTE_REPO="${REMOTE_REPO:-/home/user/cloud-connector-routing-adapter}"
REMOTE_KUBECTL="${REMOTE_KUBECTL:-/var/lib/rancher/rke2/bin/kubectl}"
REMOTE_KUBECONFIG="${REMOTE_KUBECONFIG:-/etc/rancher/rke2/rke2.yaml}"
VYOS_URL="${VYOS_URL:-https://vyos.example.test}"
VYOS_API_KEY="${VYOS_API_KEY:-replace-with-real-api-key}"
VYOS_TIMEOUT="${VYOS_TIMEOUT:-90}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-240}"
WAIT_INTERVAL="${WAIT_INTERVAL:-5}"
ROUNDS="${ROUNDS:-2}"
MATRIX_CYCLES="${MATRIX_CYCLES:-1}"
RUN_DEEPER_HARNESS="${RUN_DEEPER_HARNESS:-1}"
DEEPER_ITERATIONS="${DEEPER_ITERATIONS:-1}"
BASE_NAMESPACE="${BASE_NAMESPACE:-hbr-api-lifecycle}"
ARTIFACT_DIR="${ARTIFACT_DIR:-$repo_root/artifacts/private/hbr-lifecycle}"
MATRIX_SCRIPT="${repo_root}/scripts/api-parameter-matrix-vyos-vm.sh"
DEEPER_SCRIPT="${repo_root}/scripts/deeper-smoke-vyos-vm.sh"

usage() {
  cat <<'EOF'
Usage:
  scripts/lifecycle-api-matrix-vyos-vm.sh run

Environment:
  VENUS_HOST       SSH target for the lab host (default: user@venus.example.test)
  VM_NAME          Libvirt domain name (default: vyos-vpp-lab)
  VYOS_URL         VyOS API URL (default: https://vyos.example.test)
  VYOS_API_KEY     VyOS API key (default: replace-with-real-api-key)
  VYOS_TIMEOUT     VyOS API timeout in seconds for matrix runs (default: 90)
  WAIT_TIMEOUT     Seconds to wait for VM lifecycle transitions (default: 240)
  WAIT_INTERVAL    Poll interval for lifecycle transitions (default: 5)
  ROUNDS           Number of full lifecycle rounds to run (default: 2)
  MATRIX_CYCLES    Number of matrix cycles to run after each lifecycle phase (default: 1)
  RUN_DEEPER_HARNESS
                    Run the deeper smoke harness after each lifecycle phase (default: 1)
  DEEPER_ITERATIONS
                    Iterations for the deeper smoke harness per lifecycle phase (default: 1)
  BASE_NAMESPACE   Kubernetes namespace prefix per lifecycle phase (default: hbr-api-lifecycle)
  ARTIFACT_DIR     Local root for lifecycle artifacts (default: artifacts/private/hbr-lifecycle)

Phases:
  baseline         Run the API matrix against the currently running VM
  reboot           virsh reboot, wait for API to disappear and return, rerun matrix
  reset            virsh reset, wait for API to disappear and return, rerun matrix
  stop-start       virsh shutdown/start, wait for stop and boot, rerun matrix
EOF
}

log() {
  printf '\n==> %s\n' "$*"
}

remote_run() {
  ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
    "$VENUS_HOST" "$1"
}

check_venus_access() {
  log "Checking SSH access to Venus"
  remote_run "printf 'venus-ok\n'" >/dev/null
}

check_executable_script() {
  local path="$1"
  local label="$2"
  if [[ ! -x "$path" ]]; then
    echo "E: ${label} is not executable: $path" >&2
    exit 1
  fi
}

domain_state() {
  remote_run "sudo virsh domstate '${VM_NAME}' 2>/dev/null || true" | tr -d '\r'
}

wait_for_domain_state() {
  local target="$1"
  local context="$2"
  local deadline=$((SECONDS + WAIT_TIMEOUT))
  local state
  while ((SECONDS < deadline)); do
    state="$(domain_state)"
    if [[ "$state" == *"$target"* ]]; then
      return 0
    fi
    sleep "$WAIT_INTERVAL"
  done
  echo "E: timed out waiting for domain ${VM_NAME} to reach state '${target}' during ${context}" >&2
  echo "E: last observed state: ${state:-unknown}" >&2
  return 1
}

vyos_api_ok() {
  curl -ksS --connect-timeout 5 --max-time 10 \
    --form 'data={"op":"showConfig","path":[]}' \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >/dev/null
}

wait_for_vyos_api_up() {
  local context="$1"
  local deadline=$((SECONDS + WAIT_TIMEOUT))
  while ((SECONDS < deadline)); do
    if vyos_api_ok; then
      return 0
    fi
    sleep "$WAIT_INTERVAL"
  done
  echo "E: timed out waiting for VyOS API to come back during ${context}" >&2
  return 1
}

wait_for_vyos_api_down() {
  local context="$1"
  local deadline=$((SECONDS + WAIT_TIMEOUT))
  while ((SECONDS < deadline)); do
    if ! vyos_api_ok; then
      return 0
    fi
    sleep "$WAIT_INTERVAL"
  done
  echo "E: timed out waiting for VyOS API to go down during ${context}" >&2
  return 1
}

collect_phase_snapshots() {
  local phase_dir="$1"
  local context="$2"
  local safe_context="${context//_/-}"
  mkdir -p "$phase_dir"
  remote_run "sudo virsh dominfo '${VM_NAME}'" >"${phase_dir}/dominfo-${safe_context}.txt"
  remote_run "sudo virsh domstate '${VM_NAME}'" >"${phase_dir}/domstate-${safe_context}.txt"
  curl -ksS --connect-timeout 5 --max-time 20 \
    --form 'data={"op":"showConfig","path":[]}' \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >"${phase_dir}/config-${safe_context}.json"
  curl -ksS --connect-timeout 5 --max-time 20 \
    --form 'data={"op":"showConfig","path":["interfaces","ethernet","eth1"]}' \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >"${phase_dir}/eth1-${safe_context}.json"
  curl -ksS --connect-timeout 5 --max-time 20 \
    --form 'data={"op":"showConfig","path":["service","https"]}' \
    --form "key=${VYOS_API_KEY}" \
    "${VYOS_URL%/}/retrieve" >"${phase_dir}/https-${safe_context}.json"
}

run_matrix_phase() {
  local round="$1"
  local phase="$2"
  local safe_phase="${phase//_/-}"
  local phase_dir="${ARTIFACT_DIR}/round-${round}/${safe_phase}"
  local phase_artifacts="${phase_dir}/matrix"
  local phase_namespace="${BASE_NAMESPACE}-r${round}-${safe_phase}"
  local phase_workdir="/tmp/hbr-lifecycle-r${round}-${safe_phase}-matrix"

  mkdir -p "$phase_artifacts"
  log "Running API matrix for lifecycle phase ${phase} in round ${round}"
  CYCLES="$MATRIX_CYCLES" \
  TEST_NAMESPACE="$phase_namespace" \
  ARTIFACT_DIR="$phase_artifacts" \
  LOCAL_WORKDIR="$phase_workdir" \
  VYOS_TIMEOUT="$VYOS_TIMEOUT" \
  VENUS_HOST="$VENUS_HOST" \
  REMOTE_REPO="$REMOTE_REPO" \
  REMOTE_KUBECTL="$REMOTE_KUBECTL" \
  REMOTE_KUBECONFIG="$REMOTE_KUBECONFIG" \
  VYOS_URL="$VYOS_URL" \
  VYOS_API_KEY="$VYOS_API_KEY" \
  "$MATRIX_SCRIPT" run
}

run_deeper_phase() {
  local round="$1"
  local phase="$2"
  local safe_phase="${phase//_/-}"
  local phase_artifacts="${ARTIFACT_DIR}/round-${round}/${safe_phase}/deeper"
  local phase_namespace="${BASE_NAMESPACE}-deep-r${round}-${safe_phase}"
  local phase_workdir="/tmp/hbr-lifecycle-r${round}-${safe_phase}-deeper"

  if [[ "$RUN_DEEPER_HARNESS" != "1" ]]; then
    return 0
  fi

  mkdir -p "$phase_artifacts"
  log "Running deeper smoke for lifecycle phase ${phase} in round ${round}"
  ITERATIONS="$DEEPER_ITERATIONS" \
  TEST_NAMESPACE="$phase_namespace" \
  ARTIFACT_DIR="$phase_artifacts" \
  LOCAL_WORKDIR="$phase_workdir" \
  VENUS_HOST="$VENUS_HOST" \
  REMOTE_REPO="$REMOTE_REPO" \
  REMOTE_KUBECTL="$REMOTE_KUBECTL" \
  REMOTE_KUBECONFIG="$REMOTE_KUBECONFIG" \
  VYOS_URL="$VYOS_URL" \
  VYOS_API_KEY="$VYOS_API_KEY" \
  "$DEEPER_SCRIPT" run
}

run_phase_checks() {
  local round="$1"
  local phase="$2"
  local safe_phase="${phase//_/-}"
  local phase_dir="${ARTIFACT_DIR}/round-${round}/${safe_phase}"

  mkdir -p "$phase_dir"
  collect_phase_snapshots "$phase_dir" "pre-checks"
  run_matrix_phase "$round" "$phase"
  run_deeper_phase "$round" "$phase"
  collect_phase_snapshots "$phase_dir" "post-checks"
}

do_reboot_phase() {
  local round="$1"
  log "Requesting graceful guest reboot for ${VM_NAME} in round ${round}"
  remote_run "sudo virsh reboot '${VM_NAME}'"
  wait_for_vyos_api_down "reboot"
  wait_for_domain_state "running" "reboot"
  wait_for_vyos_api_up "reboot"
  run_phase_checks "$round" "reboot"
}

do_reset_phase() {
  local round="$1"
  log "Requesting hard guest reset for ${VM_NAME} in round ${round}"
  remote_run "sudo virsh reset '${VM_NAME}'"
  wait_for_vyos_api_down "reset"
  wait_for_domain_state "running" "reset"
  wait_for_vyos_api_up "reset"
  run_phase_checks "$round" "reset"
}

do_stop_start_phase() {
  local round="$1"
  log "Requesting graceful shutdown for ${VM_NAME} in round ${round}"
  remote_run "sudo virsh shutdown '${VM_NAME}'"
  if ! wait_for_domain_state "shut off" "stop-start shutdown"; then
    log "Graceful shutdown timed out, forcing power off"
    remote_run "sudo virsh destroy '${VM_NAME}'"
    wait_for_domain_state "shut off" "stop-start destroy"
  fi
  wait_for_vyos_api_down "stop-start shutdown"
  log "Starting ${VM_NAME} again"
  remote_run "sudo virsh start '${VM_NAME}'"
  wait_for_domain_state "running" "stop-start boot"
  wait_for_vyos_api_up "stop-start boot"
  run_phase_checks "$round" "stop-start"
}

main() {
  local phase="${1:-}"
  if [[ "$phase" != "run" ]]; then
    usage
    exit 1
  fi

  check_executable_script "$MATRIX_SCRIPT" "matrix script"
  check_executable_script "$DEEPER_SCRIPT" "deeper smoke script"
  check_venus_access
  mkdir -p "$ARTIFACT_DIR"
  wait_for_domain_state "running" "baseline preflight"
  wait_for_vyos_api_up "baseline preflight"
  local round
  for ((round = 1; round <= ROUNDS; round++)); do
    log "Starting lifecycle round ${round}/${ROUNDS}"
    run_phase_checks "$round" "baseline"
    do_reboot_phase "$round"
    do_reset_phase "$round"
    do_stop_start_phase "$round"
  done

  log "Lifecycle API matrix completed successfully across ${ROUNDS} rounds"
  log "Artifacts saved under ${ARTIFACT_DIR}"
}

main "$@"
