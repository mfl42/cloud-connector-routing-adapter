#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
python_bin="${repo_root}/.venv/bin/python3"
if [[ ! -x "$python_bin" ]]; then
  python_bin="python3"
fi

usage() {
  cat <<'EOF'
Usage:
  scripts/smoke-vyos-vm.sh <phase>

Phases:
  plan       Run local translation for the safe smoke manifests
  api-test   Check the VyOS HTTPS API with curl
  apply      Apply the safe smoke manifests directly to VyOS
  all        Run plan, api-test, and apply

Required environment for api-test/apply:
  VYOS_URL      Example: https://vyos.example.test
  VYOS_API_KEY  VyOS HTTPS API key

Optional environment:
  STATE_FILE    Default: /tmp/hbr-smoke-state.json
  STATUS_FILE   Default: /tmp/hbr-smoke-status.json
EOF
}

phase="${1:-}"
if [[ -z "$phase" ]]; then
  usage
  exit 1
fi

state_file="${STATE_FILE:-/tmp/hbr-smoke-state.json}"
status_file="${STATUS_FILE:-/tmp/hbr-smoke-status.json}"

plan_phase() {
  "$python_bin" -m hbr_vyos_adapter.cli \
    plan --file examples/node-network-config-smoke.json
  "$python_bin" -m hbr_vyos_adapter.cli \
    plan --file examples/node-netplan-config-smoke.json
}

api_test_phase() {
  : "${VYOS_URL:?VYOS_URL is required}"
  : "${VYOS_API_KEY:?VYOS_API_KEY is required}"
  curl -ksS --location --request POST "${VYOS_URL%/}/retrieve" \
    --form data='{"op": "showConfig", "path": []}' \
    --form "key=${VYOS_API_KEY}"
}

apply_phase() {
  : "${VYOS_URL:?VYOS_URL is required}"
  : "${VYOS_API_KEY:?VYOS_API_KEY is required}"
  "$python_bin" -m hbr_vyos_adapter.cli \
    reconcile --file examples/node-network-config-smoke.json \
    --state-file "$state_file" \
    --status-file "$status_file" \
    --apply \
    --vyos-url "$VYOS_URL" \
    --api-key "$VYOS_API_KEY"
  "$python_bin" -m hbr_vyos_adapter.cli \
    reconcile --file examples/node-netplan-config-smoke.json \
    --state-file "$state_file" \
    --status-file "$status_file" \
    --apply \
    --vyos-url "$VYOS_URL" \
    --api-key "$VYOS_API_KEY"
}

case "$phase" in
  plan)
    plan_phase
    ;;
  api-test)
    api_test_phase
    ;;
  apply)
    apply_phase
    ;;
  all)
    plan_phase
    api_test_phase
    apply_phase
    ;;
  *)
    usage
    exit 1
    ;;
esac
