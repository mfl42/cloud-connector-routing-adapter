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

    def configure_commands(self, commands: list[str]) -> dict:
        self.calls.append(list(commands))
        if self._outcomes:
            outcome = self._outcomes.pop(0)
        else:
            outcome = {"success": True, "operations": [{"success": True}]}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


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
