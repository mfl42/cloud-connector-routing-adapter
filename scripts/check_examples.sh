#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

tmp_dir="$(mktemp -d)"
server_pid=""
cleanup() {
  if [ -n "$server_pid" ]; then
    kill "$server_pid" 2>/dev/null || true
  fi
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

python3 -m hbr_vyos_adapter.cli \
  plan --file examples/node-network-config.json \
  >"$tmp_dir/node-network-config.plan.txt"

python3 -m hbr_vyos_adapter.cli \
  plan --file examples/node-netplan-config.json \
  >"$tmp_dir/node-netplan-config.plan.txt"

diff -u \
  examples/expected/node-network-config.plan.txt \
  "$tmp_dir/node-network-config.plan.txt"

diff -u \
  examples/expected/node-netplan-config.plan.txt \
  "$tmp_dir/node-netplan-config.plan.txt"

state_file="$tmp_dir/reconcile-state.json"
status_file="$tmp_dir/reconcile-status.json"
python3 -m hbr_vyos_adapter.cli \
  reconcile --file examples/node-network-config.json \
  --state-file "$state_file" --status-file "$status_file" --json \
  >"$tmp_dir/node-network-config.reconcile.json"

python3 -m hbr_vyos_adapter.cli \
  status --state-file "$state_file" --json \
  >"$tmp_dir/node-network-config.status.json"

python3 -m hbr_vyos_adapter.cli \
  write-status --state-file "$state_file" --dry-run --json \
  >"$tmp_dir/node-network-config.write-status.json"

python3 -m hbr_vyos_adapter.cli \
  controller --file examples/node-network-config.json \
  --state-file "$tmp_dir/controller-state.json" \
  --status-file "$tmp_dir/controller-status.json" \
  --once \
  --write-status \
  --dry-run-status \
  --json \
  >"$tmp_dir/node-network-config.controller.json"

python3 - "$tmp_dir/node-network-config.reconcile.json" "$state_file" "$status_file" "$tmp_dir/node-network-config.status.json" "$tmp_dir/node-network-config.write-status.json" "$tmp_dir/node-network-config.controller.json" <<'PY'
import json
import sys

result = json.load(open(sys.argv[1]))
state = json.load(open(sys.argv[2]))
status_file = json.load(open(sys.argv[3]))
status_cli = json.load(open(sys.argv[4]))
write_status = json.load(open(sys.argv[5]))
controller = json.load(open(sys.argv[6]))

assert result["apply_requested"] is False
assert result["apply_performed"] is False
assert result["command_count"] == 20
assert len(result["documents"]) == 1
assert result["status_file"]
assert result["status_report"]["documentCount"] == 1

document = result["documents"][0]
assert document["key"] == "NodeNetworkConfig:default/vyos-lab-node"
assert document["desired_revision"] == "rev-0001"
assert document["action"] == "pending-apply"
assert document["in_sync"] is False
assert document["warning_count"] == 2
assert document["unsupported_count"] == 0
assert len(document["desired_digest"]) == 64

saved = state["documents"]["NodeNetworkConfig:default/vyos-lab-node"]
assert saved["desired_revision"] == "rev-0001"
assert saved["applied_revision"] is None
assert saved["last_result"] == "pending-apply"
assert len(saved["desired_digest"]) == 64

for report in (status_file, status_cli, result["status_report"]):
    assert report["apiVersion"] == "adapter.mfl42.io/v1alpha1"
    assert report["kind"] == "AdapterStatusReport"
    assert report["documentCount"] == 1
    status_doc = report["documents"][0]
    assert status_doc["phase"] == "PendingApply"
    assert status_doc["desired_revision"] == "rev-0001"
    assert status_doc["applied_revision"] is None
    assert status_doc["warning_count"] == 2
    assert status_doc["unsupported_count"] == 0
    condition_types = {item["type"] for item in status_doc["conditions"]}
    assert {"DesiredSeen", "Applied", "InSync", "HasWarnings"} <= condition_types

assert write_status["dry_run"] is True
assert len(write_status["patches"]) == 1
patch = write_status["patches"][0]
assert patch["apiVersion"] == "network.t-caas.telekom.com/v1alpha1"
assert patch["kind"] == "NodeNetworkConfig"
assert patch["plural"] == "nodenetworkconfigs"
assert patch["url"] == "https://kubernetes.default.svc/apis/network.t-caas.telekom.com/v1alpha1/namespaces/default/nodenetworkconfigs/vyos-lab-node/status"
assert patch["body"]["status"]["phase"] == "PendingApply"
assert patch["body"]["status"]["observedRevision"] == "rev-0001"
assert patch["body"]["status"]["warningCount"] == 2

assert controller["once"] is True
assert len(controller["iterations"]) == 1
iteration = controller["iterations"][0]
assert iteration["ok"] is True
assert iteration["changed_documents"] == 1
assert iteration["pending_command_count"] == 20
assert iteration["status_patch_count"] == 1
assert iteration["apply_performed"] is False
assert iteration["status_write_performed"] is False
PY

mkdir -p "$tmp_dir/fake_requests"
cat >"$tmp_dir/fake_requests/requests.py" <<'PY'
import json
import os
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

repo_root = Path(os.environ["FAKE_K8S_REPO_ROOT"])

nnc = json.loads((repo_root / "examples/node-network-config.json").read_text())
nnc.setdefault("metadata", {})["resourceVersion"] = "100"
nnc["metadata"]["generation"] = 7
nnc_modified = json.loads(json.dumps(nnc))
nnc_modified["metadata"]["resourceVersion"] = "101"

nnpc = json.loads((repo_root / "examples/node-netplan-config.json").read_text())
nnpc.setdefault("metadata", {})["resourceVersion"] = "200"
nnpc["metadata"]["generation"] = 3
nnc_watch_calls = 0
nnpc_watch_calls = 0
patch_calls = 0


class Response:
    def __init__(self, status_code=200, payload=None, lines=None):
        self.status_code = status_code
        self._payload = payload or {}
        self._lines = lines or []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"fake HTTP {self.status_code}")

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line if decode_unicode else line.encode()


def get(url, headers=None, params=None, timeout=None, verify=None, stream=False):
    global nnc_watch_calls, nnpc_watch_calls
    parsed = urlsplit(url)
    effective = params or parse_qs(parsed.query)
    watch = effective.get("watch") in ("true", ["true"])

    if parsed.path == "/apis/network.t-caas.telekom.com/v1alpha1/nodenetworkconfigs":
        if watch:
            nnc_watch_calls += 1
            if nnc_watch_calls == 1:
                return Response(
                    lines=[
                        json.dumps(
                            {
                                "type": "ERROR",
                                "object": {
                                    "code": 410,
                                    "reason": "Expired",
                                    "message": "too old resource version",
                                },
                            }
                        )
                    ]
                )
            if nnc_watch_calls == 2:
                return Response(lines=[json.dumps({"type": "MODIFIED", "object": nnc_modified})])
            return Response(lines=[])
        return Response(
            payload={
                "apiVersion": "network.t-caas.telekom.com/v1alpha1",
                "kind": "NodeNetworkConfigList",
                "metadata": {"resourceVersion": "100"},
                "items": [nnc],
            }
        )

    if parsed.path == "/apis/network.t-caas.telekom.com/v1alpha1/nodenetplanconfigs":
        if watch:
            nnpc_watch_calls += 1
            if nnpc_watch_calls == 1:
                return Response(lines=[json.dumps({"type": "DELETED", "object": nnpc})])
            return Response(lines=[])
        return Response(
            payload={
                "apiVersion": "network.t-caas.telekom.com/v1alpha1",
                "kind": "NodeNetplanConfigList",
                "metadata": {"resourceVersion": "200"},
                "items": [nnpc],
            }
        )

    return Response(status_code=404, payload={"url": url, "params": params})


def patch(url, headers=None, data=None, timeout=None, verify=None):
    global patch_calls
    patch_calls += 1
    if patch_calls == 1:
        return Response(status_code=409, payload={"patched": False, "call": patch_calls})
    return Response(payload={"patched": True, "url": url, "data": data, "call": patch_calls})
PY

FAKE_K8S_REPO_ROOT="$repo_root" PYTHONPATH="$tmp_dir/fake_requests:." python3 -m hbr_vyos_adapter.cli \
  controller \
  --source kubernetes \
  --server "http://fake-k8s.local" \
  --state-file "$tmp_dir/k8s-controller-state.json" \
  --status-file "$tmp_dir/k8s-controller-status.json" \
  --max-iterations 3 \
  --write-status \
  --json \
  >"$tmp_dir/node-network-config.controller-k8s.json"

python3 - "$tmp_dir/node-network-config.controller-k8s.json" "$tmp_dir/k8s-controller-state.json" "$tmp_dir/k8s-controller-status.json" <<'PY'
import json
import sys

controller = json.load(open(sys.argv[1]))
state = json.load(open(sys.argv[2]))
status_report = json.load(open(sys.argv[3]))

assert controller["source"] == "kubernetes"
assert controller["once"] is False
assert len(controller["iterations"]) == 3

for iteration in controller["iterations"]:
    assert iteration["ok"] is True
    assert iteration["apply_performed"] is False
    assert iteration["status_write_performed"] is True

first_iteration = controller["iterations"][0]
second_iteration = controller["iterations"][1]
third_iteration = controller["iterations"][2]

assert first_iteration["changed_documents"] == 2
assert first_iteration["deleted_documents"] == 0
assert first_iteration["pruned_documents"] == 0
assert first_iteration["pending_command_count"] == 24
assert first_iteration["status_patch_count"] == 2
assert second_iteration["changed_documents"] == 1
assert second_iteration["deleted_documents"] == 0
assert second_iteration["pruned_documents"] == 0
assert second_iteration["pending_command_count"] == 20
assert second_iteration["status_patch_count"] == 2
assert third_iteration["changed_documents"] == 1
assert third_iteration["deleted_documents"] == 1
assert third_iteration["pruned_documents"] == 0
assert third_iteration["pending_command_count"] == 0
assert third_iteration["status_patch_count"] == 1

responses = first_iteration["status_write"]["responses"]
assert len(responses) == 2
assert responses[0]["call"] == 2
assert responses[1]["call"] == 3

first_patch = first_iteration["status_write"]["patches"][1]
assert first_patch["body"]["metadata"]["resourceVersion"] == "100"
assert first_patch["body"]["status"]["observedGeneration"] == 7

second_patch = second_iteration["status_write"]["patches"][1]
assert second_patch["body"]["metadata"]["resourceVersion"] == "101"
assert second_patch["body"]["status"]["observedGeneration"] == 7

assert third_iteration["status_write"]["skipped"] == [
    {
        "key": "NodeNetplanConfig:default/vyos-lab-node",
        "reason": "deleted-document-local-status-only",
    }
]
third_responses = third_iteration["status_write"]["responses"]
assert len(third_responses) == 1
assert third_responses[0]["call"] == 6

saved_deleted = state["documents"]["NodeNetplanConfig:default/vyos-lab-node"]
assert saved_deleted["deleted"] is True
assert saved_deleted["last_result"] == "deleted"
assert saved_deleted["deleted_at"]

assert status_report["documentCount"] == 2
status_docs = {item["key"]: item for item in status_report["documents"]}
deleted_doc = status_docs["NodeNetplanConfig:default/vyos-lab-node"]
assert deleted_doc["phase"] == "Deleted"
assert deleted_doc["deleted"] is True
condition_types = {item["type"] for item in deleted_doc["conditions"]}
assert "Deleted" in condition_types
PY

python3 scripts/chaos-hbr-api-local.py run \
  >"$tmp_dir/chaos-summary.json" \
  2>"$tmp_dir/chaos.log"

python3 - "$tmp_dir/chaos-summary.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1]))
assert payload["scenarioCount"] == 4, payload
names = {item["scenario"] for item in payload["scenarios"]}
assert names == {
    "vyos-timeout-recovery",
    "status-writer-failure-recovery",
    "watch-churn-and-prune",
    "k8s-patch-retry",
}, names
PY

python3 scripts/boundary-hbr-api-local.py \
  >"$tmp_dir/boundary-summary.json"

python3 - "$tmp_dir/boundary-summary.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1]))
assert payload["scenarioCount"] == 7, payload
names = {item["scenario"] for item in payload["scenarios"]}
assert names == {
    "interface-boundaries",
    "static-and-policy-boundaries",
    "bgp-boundaries",
    "netplan-boundaries",
    "invalid-value-boundaries",
    "zero-command-reconcile-boundary",
    "malformed-structure-boundaries",
}, names
PY

python3 scripts/fuzz-hbr-api-local.py run \
  --iterations 40 \
  >"$tmp_dir/fuzz-summary.json"

python3 - "$tmp_dir/fuzz-summary.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1]))
assert payload["caseCount"] == 40, payload
assert payload["iterations"] == 40, payload
assert payload["maxCommandCount"] >= 0, payload
assert payload["totalCommandCount"] >= 0, payload
PY

python3 - "$tmp_dir" <<'PY'
import json
import sys
from pathlib import Path

from hbr_vyos_adapter.models import load_document
from hbr_vyos_adapter.reconcile import reconcile_documents
from hbr_vyos_adapter.state import ReconcileState
from hbr_vyos_adapter.translator import VyosTranslator

tmp_dir = Path(sys.argv[1])
state_file = tmp_dir / "revision-only-state.json"

base = {
    "apiVersion": "network.t-caas.telekom.com/v1alpha1",
    "kind": "NodeNetworkConfig",
    "metadata": {"name": "revision-only", "namespace": "default"},
    "spec": {
        "revision": "rev-1",
        "localVRFs": {
            "tenant-a": {
                "table": 1000,
                "staticRoutes": [
                    {
                        "prefix": "10.10.10.0/24",
                        "nextHop": {"address": "192.0.2.1"},
                    }
                ],
            }
        },
    },
}

updated = json.loads(json.dumps(base))
updated["spec"]["revision"] = "rev-2"

translator = VyosTranslator()
first = reconcile_documents(
    [load_document(base)],
    translator,
    ReconcileState(),
    str(state_file),
    apply=False,
)
assert first.command_count == 2, first.to_dict()

state = ReconcileState.load(state_file)
saved = state.documents["NodeNetworkConfig:default/revision-only"]
saved.applied_revision = saved.desired_revision
saved.applied_digest = saved.desired_digest
saved.last_result = "applied"
state.save(state_file)

second = reconcile_documents(
    [load_document(updated)],
    translator,
    ReconcileState.load(state_file),
    str(state_file),
    apply=True,
)
assert second.command_count == 0, second.to_dict()
assert second.apply_performed is False, second.to_dict()
doc = second.documents[0]
assert doc.desired_revision == "rev-2", doc.to_dict()
assert doc.applied_revision == "rev-2", doc.to_dict()
assert doc.in_sync is True, doc.to_dict()
assert doc.action == "applied", doc.to_dict()
PY

echo "example plans match expected output"
