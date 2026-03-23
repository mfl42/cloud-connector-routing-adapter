#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import shutil
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hbr_vyos_adapter.controller import DocumentSource
from hbr_vyos_adapter.controller import SourceUpdate
from hbr_vyos_adapter.controller import run_controller
from hbr_vyos_adapter.k8s_lease import LeaseManager
from hbr_vyos_adapter.k8s_status import KubeConnection
from hbr_vyos_adapter.k8s_status import KubeStatusWriter
from hbr_vyos_adapter.k8s_status import StatusPatchPlan
from hbr_vyos_adapter.k8s_status import StatusWriteResult
from hbr_vyos_adapter.loader import load_documents
from hbr_vyos_adapter.models import NodeNetplanConfig
from hbr_vyos_adapter.models import NodeNetworkConfig
from hbr_vyos_adapter.models import load_document
from hbr_vyos_adapter.reconcile import _document_key
from hbr_vyos_adapter.reconcile import reconcile_documents
from hbr_vyos_adapter.state import ReconcileState
from hbr_vyos_adapter.status import build_status_report
from hbr_vyos_adapter.translator import VyosTranslator


ARTIFACT_DIR = REPO_ROOT / "artifacts" / "private" / "hbr-chaos-local"
EXAMPLE_NETWORK = REPO_ROOT / "examples/node-network-config.json"
EXAMPLE_NETPLAN = REPO_ROOT / "examples/node-netplan-config.json"


class StaticSource(DocumentSource):
    name = "chaos-static"

    def __init__(self, documents: list[NodeNetworkConfig | NodeNetplanConfig]) -> None:
        self._documents = list(documents)

    def initial_update(self) -> SourceUpdate:
        keys = {_document_key(document) for document in self._documents}
        return SourceUpdate(
            documents=list(self._documents),
            changed_keys=set(keys),
            current_keys=set(keys),
        )

    def wait_for_update(self, timeout_seconds: float) -> SourceUpdate | None:
        return None


class ScriptedSource(DocumentSource):
    name = "chaos-scripted"

    def __init__(
        self,
        initial_documents: list[NodeNetworkConfig | NodeNetplanConfig],
        updates: list[SourceUpdate | Exception],
    ) -> None:
        self._initial_documents = list(initial_documents)
        self._updates = list(updates)

    def initial_update(self) -> SourceUpdate:
        keys = {_document_key(document) for document in self._initial_documents}
        return SourceUpdate(
            documents=list(self._initial_documents),
            changed_keys=set(keys),
            current_keys=set(keys),
        )

    def wait_for_update(self, timeout_seconds: float) -> SourceUpdate | None:
        if not self._updates:
            return None
        next_item = self._updates.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


class ScriptedVyosClient:
    def __init__(self, outcomes: list[dict | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[list[str]] = []
        self.discard_calls: int = 0

    def configure_commands(self, commands: list[str]) -> dict:
        self.calls.append(list(commands))
        if self._outcomes:
            outcome = self._outcomes.pop(0)
        else:
            outcome = {"success": True, "operations": [{"success": True}]}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def discard_pending(self) -> dict:
        self.discard_calls += 1
        return {"success": True}


class ScriptedStatusWriter:
    def __init__(self, outcomes: list[StatusWriteResult | Exception | None]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    def write_status(
        self,
        report,
        *,
        dry_run: bool = False,
        selector: dict[str, str | None] | None = None,
        cluster_scoped: bool = False,
        include_deleted: bool = False,
    ) -> StatusWriteResult:
        self.calls.append(
            {
                "document_count": report.document_count,
                "dry_run": dry_run,
                "cluster_scoped": cluster_scoped,
            }
        )
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, StatusWriteResult):
            return outcome

        patches: list[StatusPatchPlan] = []
        skipped: list[dict[str, str]] = []
        for document in report.documents:
            if document.deleted and not include_deleted:
                skipped.append(
                    {
                        "key": document.key,
                        "reason": "deleted-document-local-status-only",
                    }
                )
                continue
            patches.append(
                StatusPatchPlan(
                    key=document.key,
                    api_version=document.api_version,
                    kind=document.kind,
                    namespace=document.namespace or "default",
                    name=document.name,
                    plural=document.kind.lower() + "s",
                    url=(
                        f"https://fake-k8s.local/apis/network.t-caas.telekom.com/v1alpha1/"
                        f"namespaces/{document.namespace or 'default'}/"
                        f"{document.kind.lower() + 's'}/{document.name}/status"
                    ),
                    body={"status": {"phase": document.phase}},
                    cluster_scoped=cluster_scoped,
                )
            )

        return StatusWriteResult(
            dry_run=dry_run,
            patches=patches,
            skipped=skipped,
            responses=[] if dry_run else [{"patched": True, "count": len(patches)}],
        )


class FakePatchResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeRequestsModule:
    def __init__(self, outcomes: list[FakePatchResponse | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    def patch(self, url: str, **kwargs) -> FakePatchResponse:
        self.calls.append(
            {
                "url": url,
                "timeout": kwargs.get("timeout"),
                "verify": kwargs.get("verify"),
                "has_cert": kwargs.get("cert") is not None,
            }
        )
        if not self._outcomes:
            return FakePatchResponse(200, {"patched": True})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@contextmanager
def patched_requests(fake_module: FakeRequestsModule):
    original = sys.modules.get("requests")
    sys.modules["requests"] = fake_module
    try:
        yield
    finally:
        if original is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = original


def load_base_documents() -> tuple[NodeNetworkConfig, NodeNetplanConfig]:
    network = load_documents(EXAMPLE_NETWORK)[0]
    netplan = load_documents(EXAMPLE_NETPLAN)[0]
    return network, netplan


def clone_network_config(
    document: NodeNetworkConfig,
    *,
    revision: str,
    generation: int,
    resource_version: str,
    extra_route_prefix: str | None = None,
) -> NodeNetworkConfig:
    raw = copy.deepcopy(document.raw)
    raw.setdefault("metadata", {})
    raw["metadata"]["generation"] = generation
    raw["metadata"]["resourceVersion"] = resource_version
    raw["spec"]["revision"] = revision
    if extra_route_prefix:
        raw["spec"]["localVRFs"]["tenant-a"]["staticRoutes"].append(
            {
                "prefix": extra_route_prefix,
                "nextHop": {"address": "192.0.2.99"},
            }
        )
    return load_document(raw)


def clone_netplan_config(
    document: NodeNetplanConfig,
    *,
    generation: int,
    resource_version: str,
    nameservers: list[str],
) -> NodeNetplanConfig:
    raw = copy.deepcopy(document.raw)
    raw.setdefault("metadata", {})
    raw["metadata"]["generation"] = generation
    raw["metadata"]["resourceVersion"] = resource_version
    raw.setdefault("spec", {})
    raw["spec"]["nameservers"] = list(nameservers)
    return load_document(raw)


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(payload, "to_dict"):
        data = payload.to_dict()
    else:
        data = payload
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def load_state_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def reset_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def scenario_vyos_timeout_recovery() -> dict:
    scenario_dir = ARTIFACT_DIR / "vyos-timeout-recovery"
    reset_dir(scenario_dir)
    state_file = scenario_dir / "state.json"
    status_file = scenario_dir / "status.json"

    network_base, netplan_base = load_base_documents()
    network = clone_network_config(
        network_base, revision="chaos-timeout-1", generation=11, resource_version="501"
    )
    netplan = clone_netplan_config(
        netplan_base,
        generation=11,
        resource_version="601",
        nameservers=["192.0.2.53", "1.1.1.1"],
    )

    first_client = ScriptedVyosClient([TimeoutError("VyOS API read timed out during chaos test")])
    first_status_writer = ScriptedStatusWriter([])
    first_result = run_controller(
        source=StaticSource([network, netplan]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=first_client,
        write_status=True,
        status_writer=first_status_writer,
    )
    write_json(scenario_dir / "result-first.json", first_result.to_dict())

    first_iteration = first_result.iterations[0]
    assert first_iteration.ok is False, first_iteration
    assert "timed out" in (first_iteration.error or "").lower(), first_iteration

    second_client = ScriptedVyosClient([{"success": True, "operations": [{"success": True}]}])
    second_status_writer = ScriptedStatusWriter([])
    second_result = run_controller(
        source=StaticSource([network, netplan]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=second_client,
        write_status=True,
        status_writer=second_status_writer,
    )
    write_json(scenario_dir / "result-second.json", second_result.to_dict())

    second_iteration = second_result.iterations[0]
    assert second_iteration.ok is True, second_iteration
    assert second_iteration.apply_performed is True, second_iteration
    assert second_iteration.status_write_performed is True, second_iteration

    third_client = ScriptedVyosClient([])
    third_status_writer = ScriptedStatusWriter([])
    third_result = run_controller(
        source=StaticSource([network, netplan]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=third_client,
        write_status=True,
        status_writer=third_status_writer,
    )
    write_json(scenario_dir / "result-third.json", third_result.to_dict())

    third_iteration = third_result.iterations[0]
    assert third_iteration.ok is True, third_iteration
    assert third_iteration.apply_performed is False, third_iteration
    assert third_iteration.pending_command_count == 0, third_iteration

    state = ReconcileState.load(state_file)
    report = build_status_report(state).to_dict()
    write_json(scenario_dir / "status-report.json", report)
    assert report["documentCount"] == 2, report
    assert all(document["phase"] == "InSync" for document in report["documents"]), report

    return {
        "scenario": "vyos-timeout-recovery",
        "first_error": first_iteration.error,
        "second_apply_performed": second_iteration.apply_performed,
        "third_pending_command_count": third_iteration.pending_command_count,
        "vyos_call_batches": [
            len(batch) for batch in first_client.calls + second_client.calls + third_client.calls
        ],
    }


def scenario_status_writer_failure_recovery() -> dict:
    scenario_dir = ARTIFACT_DIR / "status-writer-failure-recovery"
    reset_dir(scenario_dir)
    state_file = scenario_dir / "state.json"
    status_file = scenario_dir / "status.json"

    network_base, netplan_base = load_base_documents()
    network = clone_network_config(
        network_base, revision="chaos-status-1", generation=21, resource_version="701"
    )
    netplan = clone_netplan_config(
        netplan_base,
        generation=21,
        resource_version="801",
        nameservers=["192.0.2.53", "9.9.9.9"],
    )

    first_client = ScriptedVyosClient([{"success": True, "operations": [{"success": True}]}])
    first_status_writer = ScriptedStatusWriter([RuntimeError("Kubernetes status patch 409 conflict")])
    first_result = run_controller(
        source=StaticSource([network, netplan]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=first_client,
        write_status=True,
        status_writer=first_status_writer,
    )
    write_json(scenario_dir / "result-first.json", first_result.to_dict())

    first_iteration = first_result.iterations[0]
    assert first_iteration.ok is False, first_iteration
    assert "409 conflict" in (first_iteration.error or ""), first_iteration

    state_after_failure = ReconcileState.load(state_file)
    assert state_after_failure.documents, state_after_failure
    assert all(item.applied_revision for item in state_after_failure.documents.values())

    second_client = ScriptedVyosClient([])
    second_status_writer = ScriptedStatusWriter([])
    second_result = run_controller(
        source=StaticSource([network, netplan]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=second_client,
        write_status=True,
        status_writer=second_status_writer,
    )
    write_json(scenario_dir / "result-second.json", second_result.to_dict())

    second_iteration = second_result.iterations[0]
    assert second_iteration.ok is True, second_iteration
    assert second_iteration.apply_performed is False, second_iteration
    assert second_iteration.pending_command_count == 0, second_iteration
    assert second_iteration.status_write_performed is True, second_iteration

    state = ReconcileState.load(state_file)
    report = build_status_report(state).to_dict()
    write_json(scenario_dir / "status-report.json", report)
    assert all(document["phase"] == "InSync" for document in report["documents"]), report

    return {
        "scenario": "status-writer-failure-recovery",
        "first_error": first_iteration.error,
        "state_preserved_after_failure": True,
        "second_status_write_performed": second_iteration.status_write_performed,
    }


def scenario_watch_churn_and_prune() -> dict:
    scenario_dir = ARTIFACT_DIR / "watch-churn-and-prune"
    reset_dir(scenario_dir)
    state_file = scenario_dir / "state.json"
    status_file = scenario_dir / "status.json"

    network_base, netplan_base = load_base_documents()
    network_v1 = clone_network_config(
        network_base, revision="chaos-watch-1", generation=31, resource_version="901"
    )
    netplan_v1 = clone_netplan_config(
        netplan_base,
        generation=31,
        resource_version="902",
        nameservers=["192.0.2.53", "1.1.1.1"],
    )
    network_v2 = clone_network_config(
        network_base,
        revision="chaos-watch-2",
        generation=32,
        resource_version="903",
        extra_route_prefix="203.0.113.0/24",
    )
    netplan_v2 = clone_netplan_config(
        netplan_base,
        generation=32,
        resource_version="904",
        nameservers=["192.0.2.53", "9.9.9.9", "1.1.1.1"],
    )

    nnc_key = _document_key(network_v1)
    nnpc_key = _document_key(netplan_v1)

    updates = [
        RuntimeError("Kubernetes watch timeout during chaos test"),
        SourceUpdate(
            documents=[network_v2, netplan_v2],
            changed_keys={nnc_key, nnpc_key},
            current_keys={nnc_key, nnpc_key},
        ),
        SourceUpdate(
            documents=[],
            removed_keys={nnpc_key},
            current_keys={nnc_key},
        ),
        SourceUpdate(
            documents=[],
            removed_keys={nnc_key},
            current_keys=set(),
        ),
    ]
    source = ScriptedSource([network_v1, netplan_v1], updates)
    vyos_client = ScriptedVyosClient(
        [
            {"success": True, "operations": [{"success": True}]},
            {"success": True, "operations": [{"success": True}]},
        ]
    )
    status_writer = ScriptedStatusWriter([None, None, None, None])

    result = run_controller(
        source=source,
        state_file=str(state_file),
        status_file=str(status_file),
        interval_seconds=0.01,
        once=False,
        max_iterations=4,
        apply=True,
        vyos_client=vyos_client,
        write_status=True,
        status_writer=status_writer,
        deleted_retention_seconds=0.0,
    )
    write_json(scenario_dir / "result.json", result.to_dict())

    assert len(result.iterations) == 4, result.to_dict()
    assert all(iteration.ok is True for iteration in result.iterations), result.to_dict()
    assert result.iterations[0].apply_performed is True, result.iterations[0].to_dict()
    assert result.iterations[1].apply_performed is True, result.iterations[1].to_dict()
    assert result.iterations[2].deleted_documents == 1, result.iterations[2].to_dict()
    assert result.iterations[3].deleted_documents == 1, result.iterations[3].to_dict()
    assert result.iterations[2].pruned_documents == 1, result.iterations[2].to_dict()
    assert result.iterations[3].pruned_documents == 1, result.iterations[3].to_dict()

    state = ReconcileState.load(state_file)
    write_json(scenario_dir / "state.json", load_state_dict(state_file))
    assert not state.documents, state.documents

    return {
        "scenario": "watch-churn-and-prune",
        "iteration_apply_flags": [item.apply_performed for item in result.iterations],
        "iteration_deleted_counts": [item.deleted_documents for item in result.iterations],
        "iteration_pruned_counts": [item.pruned_documents for item in result.iterations],
        "vyos_apply_calls": len(vyos_client.calls),
    }


def scenario_k8s_patch_retry() -> dict:
    scenario_dir = ARTIFACT_DIR / "k8s-patch-retry"
    reset_dir(scenario_dir)
    state_file = scenario_dir / "state.json"

    network_base, netplan_base = load_base_documents()
    network = clone_network_config(
        network_base, revision="chaos-k8s-retry-1", generation=41, resource_version="1001"
    )
    netplan = clone_netplan_config(
        netplan_base,
        generation=41,
        resource_version="1002",
        nameservers=["192.0.2.53", "8.8.8.8"],
    )

    state = ReconcileState()
    reconcile_documents(
        [network, netplan],
        VyosTranslator(),
        state,
        str(state_file),
        apply=False,
        status_file=None,
    )
    report = build_status_report(ReconcileState.load(state_file))
    write_json(scenario_dir / "status-report.json", report.to_dict())

    fake_requests = FakeRequestsModule(
        [
            RuntimeError("temporary transport loss"),
            FakePatchResponse(503, {"patched": False, "code": 503}),
            FakePatchResponse(200, {"patched": True, "code": 200}),
            FakePatchResponse(200, {"patched": True, "code": 200}),
        ]
    )
    writer = KubeStatusWriter(
        KubeConnection(server="https://fake-k8s.local", verify_tls=False),
        timeout=3,
        retry_attempts=4,
        retry_backoff_seconds=0.01,
    )
    with patched_requests(fake_requests):
        result = writer.write_status(report, dry_run=False)
    write_json(scenario_dir / "write-status.json", result.to_dict())

    assert len(result.patches) == 2, result.to_dict()
    assert len(result.responses) == 2, result.to_dict()
    assert len(fake_requests.calls) == 4, fake_requests.calls
    assert fake_requests.calls[0]["timeout"] == 3, fake_requests.calls
    assert fake_requests.calls[0]["verify"] is False, fake_requests.calls

    return {
        "scenario": "k8s-patch-retry",
        "patch_count": len(result.patches),
        "response_count": len(result.responses),
        "http_patch_calls": len(fake_requests.calls),
        "first_patch_url": fake_requests.calls[0]["url"],
    }


def scenario_commit_failure_rollback() -> dict:
    """Apply fails (commit error) → discard_pending called → state not updated → next apply retries."""
    scenario_dir = ARTIFACT_DIR / "commit-failure-rollback"
    reset_dir(scenario_dir)
    state_file  = scenario_dir / "state.json"
    status_file = scenario_dir / "status.json"

    network_base, netplan_base = load_base_documents()
    network = clone_network_config(
        network_base, revision="chaos-commit-fail-1", generation=20, resource_version="700"
    )
    netplan = clone_netplan_config(
        netplan_base, generation=20, resource_version="800",
        nameservers=["192.0.2.53"],
    )

    # First apply: VyOS returns success=False (commit error)
    fail_client = ScriptedVyosClient([{"success": False, "error": "commit failed: configuration validation error"}])
    fail_result = run_controller(
        source=StaticSource([network, netplan]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=fail_client,
        write_status=True,
        status_writer=ScriptedStatusWriter([]),
    )
    write_json(scenario_dir / "result-fail.json", fail_result.to_dict())

    # discard_pending must have been called once
    assert fail_client.discard_calls == 1, f"expected 1 discard call, got {fail_client.discard_calls}"

    # State must not have advanced (applied_revision still absent / old)
    saved_state = json.loads(state_file.read_text()) if state_file.exists() else {}
    for doc_key, entry in saved_state.get("documents", {}).items():
        assert entry.get("last_result") == "apply-failed", f"{doc_key}: expected apply-failed, got {entry.get('last_result')}"
        assert entry.get("applied_revision") is None or entry.get("applied_revision") == "", (
            f"{doc_key}: applied_revision must not advance on failure"
        )

    # Second apply: VyOS succeeds → state advances
    ok_client = ScriptedVyosClient([{"success": True, "operations": [{"success": True}]}])
    ok_result = run_controller(
        source=StaticSource([network, netplan]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=ok_client,
        write_status=True,
        status_writer=ScriptedStatusWriter([]),
    )
    write_json(scenario_dir / "result-ok.json", ok_result.to_dict())

    assert ok_result.iterations[0].ok, ok_result.to_dict()
    assert ok_client.discard_calls == 0, "no discard on success"

    return {
        "scenario": "commit-failure-rollback",
        "discard_called_on_failure": fail_client.discard_calls,
        "state_not_advanced_on_failure": True,
        "second_apply_succeeded": ok_result.iterations[0].ok,
    }


def scenario_cluster_scoped_patch_url() -> dict:
    """KubeStatusWriter._patch_plan omits /namespaces/ in the URL when cluster_scoped=True."""
    scenario_dir = ARTIFACT_DIR / "cluster-scoped-patch-url"
    reset_dir(scenario_dir)
    state_file = scenario_dir / "state.json"

    network_base, netplan_base = load_base_documents()
    network = clone_network_config(
        network_base, revision="chaos-cluster-url-1", generation=60, resource_version="1300"
    )
    netplan = clone_netplan_config(
        netplan_base,
        generation=60,
        resource_version="1400",
        nameservers=["9.9.9.9"],
    )

    state = ReconcileState()
    reconcile_documents(
        [network, netplan],
        VyosTranslator(),
        state,
        str(state_file),
        apply=False,
    )
    report = build_status_report(ReconcileState.load(state_file))

    writer = KubeStatusWriter(
        KubeConnection(server="https://fake-k8s.local", verify_tls=False),
        timeout=3,
        retry_attempts=2,
        retry_backoff_seconds=0.01,
    )

    # namespace-scoped (default): URLs must include /namespaces/
    ns_requests = FakeRequestsModule([FakePatchResponse(200)] * 4)
    with patched_requests(ns_requests):
        ns_result = writer.write_status(report, dry_run=False, cluster_scoped=False)

    assert len(ns_result.patches) == 2, ns_result.to_dict()
    for patch in ns_result.patches:
        assert "/namespaces/" in patch.url, f"expected /namespaces/ in {patch.url}"
        assert patch.cluster_scoped is False, patch.to_dict()

    # cluster-scoped: URLs must NOT include /namespaces/
    cluster_requests = FakeRequestsModule([FakePatchResponse(200)] * 4)
    with patched_requests(cluster_requests):
        cluster_result = writer.write_status(report, dry_run=False, cluster_scoped=True)

    assert len(cluster_result.patches) == 2, cluster_result.to_dict()
    for patch in cluster_result.patches:
        assert "/namespaces/" not in patch.url, f"unexpected /namespaces/ in {patch.url}"
        assert patch.cluster_scoped is True, patch.to_dict()
        assert patch.url.endswith("/status"), patch.url

    write_json(scenario_dir / "ns-patches.json", [p.to_dict() for p in ns_result.patches])
    write_json(scenario_dir / "cluster-patches.json", [p.to_dict() for p in cluster_result.patches])

    return {
        "scenario": "cluster-scoped-patch-url",
        "ns_scoped_url_sample": ns_result.patches[0].url,
        "cluster_scoped_url_sample": cluster_result.patches[0].url,
    }


def scenario_cluster_scoped_status_wiring() -> dict:
    """cluster_scoped_status=True is forwarded to the status writer on every iteration."""
    scenario_dir = ARTIFACT_DIR / "cluster-scoped-status-wiring"
    reset_dir(scenario_dir)
    state_file = scenario_dir / "state.json"
    status_file = scenario_dir / "status.json"

    network_base, netplan_base = load_base_documents()
    network = clone_network_config(
        network_base, revision="chaos-cluster-scoped-1", generation=50, resource_version="1100"
    )
    netplan = clone_netplan_config(
        netplan_base,
        generation=50,
        resource_version="1200",
        nameservers=["192.0.2.53"],
    )

    client = ScriptedVyosClient([{"success": True, "operations": [{"success": True}]}])
    status_writer = ScriptedStatusWriter([])

    result = run_controller(
        source=StaticSource([network, netplan]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=client,
        write_status=True,
        status_writer=status_writer,
        cluster_scoped_status=True,
    )
    write_json(scenario_dir / "result.json", result.to_dict())

    assert result.iterations[0].ok, result.to_dict()
    assert len(status_writer.calls) == 1, f"expected 1 status write call, got {status_writer.calls}"
    assert status_writer.calls[0]["cluster_scoped"] is True, (
        f"expected cluster_scoped=True, got {status_writer.calls[0]}"
    )

    # Second run (noop) must also forward the flag
    noop_writer = ScriptedStatusWriter([])
    run_controller(
        source=StaticSource([network, netplan]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=ScriptedVyosClient([]),
        write_status=True,
        status_writer=noop_writer,
        cluster_scoped_status=True,
    )
    assert len(noop_writer.calls) == 1, noop_writer.calls
    assert noop_writer.calls[0]["cluster_scoped"] is True, noop_writer.calls[0]

    return {
        "scenario": "cluster-scoped-status-wiring",
        "cluster_scoped_forwarded": status_writer.calls[0]["cluster_scoped"],
        "noop_cluster_scoped_forwarded": noop_writer.calls[0]["cluster_scoped"],
    }


def scenario_route_map_apply_failure_and_retry() -> dict:
    """Route-map filter commands survive a commit failure + rollback cycle."""
    scenario_dir = ARTIFACT_DIR / "route-map-apply-failure"
    reset_dir(scenario_dir)
    state_file = scenario_dir / "state.json"
    status_file = scenario_dir / "status.json"

    document = load_document({
        "apiVersion": "network.t-caas.telekom.com/v1alpha1",
        "kind": "NodeNetworkConfig",
        "metadata": {"name": "filter-chaos-node"},
        "spec": {
            "revision": "chaos-filters-1",
            "localVRFs": {
                "chaos-vrf": {
                    "table": 300,
                    "localASN": 65000,
                    "bgpPeers": [
                        {
                            "address": "192.0.2.10",
                            "remoteASN": 65010,
                            "addressFamilies": ["ipv4-unicast"],
                            "ipv4": {
                                "importFilter": {
                                    "defaultAction": {"type": "reject"},
                                    "items": [
                                        {
                                            "action": {"type": "accept"},
                                            "matcher": {"prefix": {"prefix": "10.0.0.0/8"}},
                                        }
                                    ],
                                },
                            },
                        }
                    ],
                }
            },
        },
    })

    # First apply: VyOS returns success=False
    fail_client = ScriptedVyosClient([{"success": False, "error": "commit failed"}])
    fail_result = run_controller(
        source=StaticSource([document]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=fail_client,
        write_status=True,
        status_writer=ScriptedStatusWriter([]),
    )

    assert fail_client.discard_calls == 1, fail_client.discard_calls
    # route-map commands must have been in the batch
    assert any("route-map" in c for batch in fail_client.calls for c in batch), fail_client.calls
    assert any("prefix-list" in c for batch in fail_client.calls for c in batch), fail_client.calls

    # Second apply: success
    ok_client = ScriptedVyosClient([{"success": True, "operations": [{"success": True}]}])
    ok_result = run_controller(
        source=StaticSource([document]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=ok_client,
        write_status=True,
        status_writer=ScriptedStatusWriter([]),
    )

    assert ok_result.iterations[0].ok, ok_result.to_dict()
    assert ok_result.iterations[0].apply_performed, ok_result.to_dict()
    # route-map commands present in the retry batch too
    assert any("route-map" in c for batch in ok_client.calls for c in batch), ok_client.calls

    # Third pass: noop
    noop_client = ScriptedVyosClient([])
    noop_result = run_controller(
        source=StaticSource([document]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=noop_client,
        write_status=True,
        status_writer=ScriptedStatusWriter([]),
    )

    assert noop_result.iterations[0].pending_command_count == 0, noop_result.to_dict()

    return {
        "scenario": "route-map-apply-failure-and-retry",
        "discard_on_fail": fail_client.discard_calls,
        "retry_applied": ok_result.iterations[0].apply_performed,
        "noop_after_success": noop_result.iterations[0].pending_command_count == 0,
    }


class FakeLeaseManager(LeaseManager):
    """Configurable leader election stub for testing."""

    def __init__(self, is_leader_sequence: list[bool]) -> None:
        self._sequence = list(is_leader_sequence)
        self._is_leader = False
        self._leader_id = "test-pod-1"
        self.acquire_calls = 0

    def acquire(self) -> bool:
        self.acquire_calls += 1
        if self._sequence:
            self._is_leader = self._sequence.pop(0)
        return self._is_leader

    def release(self) -> None:
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    @property
    def holder_identity(self) -> str:
        return self._leader_id


def scenario_leader_election_skip_apply() -> dict:
    """Non-leader instance skips VyOS apply; leader instance applies."""
    scenario_dir = ARTIFACT_DIR / "leader-election-skip-apply"
    reset_dir(scenario_dir)
    state_file = scenario_dir / "state.json"
    status_file = scenario_dir / "status.json"

    network_base, netplan_base = load_base_documents()
    network = clone_network_config(
        network_base, revision="chaos-leader-1", generation=70, resource_version="1500"
    )
    netplan = clone_netplan_config(
        netplan_base, generation=70, resource_version="1600",
        nameservers=["192.0.2.53"],
    )

    # Non-leader: acquire returns False → apply should NOT happen
    non_leader_client = ScriptedVyosClient([{"success": True}])
    non_leader_lease = FakeLeaseManager([False])
    non_leader_result = run_controller(
        source=StaticSource([network, netplan]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=non_leader_client,
        write_status=True,
        status_writer=ScriptedStatusWriter([]),
        lease_manager=non_leader_lease,
    )
    write_json(scenario_dir / "result-non-leader.json", non_leader_result.to_dict())

    assert non_leader_result.iterations[0].ok, non_leader_result.to_dict()
    assert non_leader_result.iterations[0].apply_performed is False, (
        "non-leader should NOT apply"
    )
    assert len(non_leader_client.calls) == 0, "non-leader should make zero VyOS calls"

    # Leader: acquire returns True → apply should happen
    leader_client = ScriptedVyosClient([{"success": True}])
    leader_lease = FakeLeaseManager([True])
    leader_result = run_controller(
        source=StaticSource([network, netplan]),
        state_file=str(state_file),
        status_file=str(status_file),
        once=True,
        apply=True,
        vyos_client=leader_client,
        write_status=True,
        status_writer=ScriptedStatusWriter([]),
        lease_manager=leader_lease,
    )
    write_json(scenario_dir / "result-leader.json", leader_result.to_dict())

    assert leader_result.iterations[0].ok, leader_result.to_dict()
    assert leader_result.iterations[0].apply_performed is True, "leader should apply"
    assert len(leader_client.calls) == 1, f"leader should make 1 VyOS call, got {len(leader_client.calls)}"

    return {
        "scenario": "leader-election-skip-apply",
        "non_leader_applied": non_leader_result.iterations[0].apply_performed,
        "leader_applied": leader_result.iterations[0].apply_performed,
        "non_leader_vyos_calls": len(non_leader_client.calls),
        "leader_vyos_calls": len(leader_client.calls),
    }


def scenario_informer_event_queue() -> dict:
    """Informer-pattern: ScriptedSource pushes events, controller consumes them immediately."""
    scenario_dir = ARTIFACT_DIR / "informer-event-queue"
    reset_dir(scenario_dir)
    state_file = scenario_dir / "state.json"
    status_file = scenario_dir / "status.json"

    network_base, netplan_base = load_base_documents()
    network_v1 = clone_network_config(
        network_base, revision="informer-1", generation=80, resource_version="1700"
    )
    netplan_v1 = clone_netplan_config(
        netplan_base, generation=80, resource_version="1800",
        nameservers=["192.0.2.53"],
    )
    network_v2 = clone_network_config(
        network_base, revision="informer-2", generation=81, resource_version="1701",
        extra_route_prefix="198.51.100.0/24",
    )

    nnc_key = _document_key(network_v1)
    nnpc_key = _document_key(netplan_v1)

    # Initial apply → then update via ScriptedSource (simulates informer push)
    updates = [
        SourceUpdate(
            documents=[network_v2],
            changed_keys={nnc_key},
            current_keys={nnc_key, nnpc_key},
        ),
    ]
    source = ScriptedSource([network_v1, netplan_v1], updates)
    vyos_client = ScriptedVyosClient([
        {"success": True, "operations": [{"success": True}]},
        {"success": True, "operations": [{"success": True}]},
    ])

    result = run_controller(
        source=source,
        state_file=str(state_file),
        status_file=str(status_file),
        interval_seconds=0.01,
        once=False,
        max_iterations=2,
        apply=True,
        vyos_client=vyos_client,
        write_status=True,
        status_writer=ScriptedStatusWriter([None, None]),
    )
    write_json(scenario_dir / "result.json", result.to_dict())

    assert len(result.iterations) == 2, result.to_dict()
    assert result.iterations[0].ok, result.iterations[0].to_dict()
    assert result.iterations[0].apply_performed, result.iterations[0].to_dict()
    assert result.iterations[1].ok, result.iterations[1].to_dict()
    assert result.iterations[1].apply_performed, result.iterations[1].to_dict()
    # Second iteration should have detected the route change
    assert result.iterations[1].changed_documents >= 1, result.iterations[1].to_dict()

    return {
        "scenario": "informer-event-queue",
        "iterations": len(result.iterations),
        "both_applied": all(i.apply_performed for i in result.iterations),
        "second_changed": result.iterations[1].changed_documents,
    }


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] not in {"run"}:
        print("usage: chaos-hbr-api-local.py [run]", file=sys.stderr)
        return 1

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    summaries = [
        scenario_vyos_timeout_recovery(),
        scenario_status_writer_failure_recovery(),
        scenario_watch_churn_and_prune(),
        scenario_k8s_patch_retry(),
        scenario_commit_failure_rollback(),
        scenario_cluster_scoped_patch_url(),
        scenario_cluster_scoped_status_wiring(),
        scenario_route_map_apply_failure_and_retry(),
        scenario_leader_election_skip_apply(),
        scenario_informer_event_queue(),
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
