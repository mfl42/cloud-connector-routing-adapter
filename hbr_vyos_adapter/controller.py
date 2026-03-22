from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field

from .k8s_documents import KubeDocumentClient
from .k8s_documents import WatchEvent
from .k8s_status import KubeStatusWriter
from .loader import load_documents
from .models import NodeNetplanConfig
from .models import NodeNetworkConfig
from .reconcile import _document_key
from .reconcile import reconcile_documents
from .reconcile import teardown_documents
from .state import ReconcileState
from .status import build_status_report
from .status import write_status_report
from .translator import VyosTranslator
from .vyos_api import VyosApiClient


@dataclass(slots=True)
class ControllerIterationResult:
    iteration: int
    ok: bool
    changed_documents: int = 0
    deleted_documents: int = 0
    pruned_documents: int = 0
    pending_command_count: int = 0
    status_patch_count: int = 0
    apply_performed: bool = False
    status_write_performed: bool = False
    error: str | None = None
    reconcile: dict | None = None
    status_write: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ControllerRunResult:
    once: bool
    interval_seconds: float
    source: str
    iterations: list[ControllerIterationResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "once": self.once,
            "interval_seconds": self.interval_seconds,
            "source": self.source,
            "iterations": [item.to_dict() for item in self.iterations],
        }


class DocumentSource:
    name = "unknown"

    def initial_update(self) -> "SourceUpdate":
        raise NotImplementedError

    def wait_for_update(self, timeout_seconds: float) -> "SourceUpdate | None":
        raise NotImplementedError


@dataclass(slots=True)
class SourceUpdate:
    documents: list[NodeNetworkConfig | NodeNetplanConfig]
    changed_keys: set[str] = field(default_factory=set)
    removed_keys: set[str] = field(default_factory=set)
    current_keys: set[str] = field(default_factory=set)


@dataclass(slots=True)
class FileDocumentSource(DocumentSource):
    file: str
    name: str = "file"
    _documents_by_key: dict[str, NodeNetworkConfig | NodeNetplanConfig] = field(default_factory=dict)
    _last_mtime: float = field(default=0.0)

    def initial_update(self) -> SourceUpdate:
        documents = load_documents(self.file)
        self._documents_by_key = {_document_key(document): document for document in documents}
        self._last_mtime = _file_mtime(self.file)
        return SourceUpdate(
            documents=documents,
            changed_keys=set(self._documents_by_key),
            current_keys=set(self._documents_by_key),
        )

    def wait_for_update(self, timeout_seconds: float) -> SourceUpdate | None:
        time.sleep(timeout_seconds)
        current_mtime = _file_mtime(self.file)
        if current_mtime == self._last_mtime:
            return None
        self._last_mtime = current_mtime
        documents = load_documents(self.file)
        next_documents = {_document_key(document): document for document in documents}
        removed = set(self._documents_by_key) - set(next_documents)
        changed = {
            key
            for key, document in next_documents.items()
            if key not in self._documents_by_key
            or self._documents_by_key[key].raw != document.raw
        }
        self._documents_by_key = next_documents
        if not changed and not removed:
            return None
        return SourceUpdate(
            documents=[next_documents[key] for key in changed],
            changed_keys=changed,
            removed_keys=removed,
            current_keys=set(next_documents),
        )

@dataclass(slots=True)
class KubernetesDocumentSource(DocumentSource):
    client: KubeDocumentClient
    namespace: str | None = None
    cluster_scoped: bool = False
    resource_kinds: list[str] | None = None
    name: str = "kubernetes"
    _resource_versions: dict[str, str] = field(default_factory=dict)
    _documents_by_key: dict[str, NodeNetworkConfig | NodeNetplanConfig] = field(default_factory=dict)
    _keys_by_kind: dict[str, set[str]] = field(default_factory=dict)

    def initial_update(self) -> SourceUpdate:
        snapshot = self.client.list_documents(
            namespace=self.namespace,
            cluster_scoped=self.cluster_scoped,
            resource_kinds=self.resource_kinds,
        )
        self._resource_versions = snapshot.resource_versions
        self._documents_by_key = {_document_key(document): document for document in snapshot.documents}
        self._keys_by_kind = _build_kind_index(self._documents_by_key)
        return SourceUpdate(
            documents=snapshot.documents,
            changed_keys=set(self._documents_by_key),
            current_keys=set(self._documents_by_key),
        )

    def wait_for_update(self, timeout_seconds: float) -> SourceUpdate | None:
        watch_result = self.client.watch_for_change(
            self._resource_versions,
            namespace=self.namespace,
            cluster_scoped=self.cluster_scoped,
            resource_kinds=self.resource_kinds,
            timeout_seconds=timeout_seconds,
        )
        self._resource_versions = watch_result.resource_versions
        if not watch_result.changed:
            return None

        changed_keys: set[str] = set()
        removed_keys: set[str] = set()
        changed_documents: dict[str, NodeNetworkConfig | NodeNetplanConfig] = {}

        if watch_result.relist_required:
            kinds_to_refresh = {event.kind for event in watch_result.events if event.kind}
            for kind in kinds_to_refresh:
                refreshed = self.client.list_documents(
                    namespace=self.namespace,
                    cluster_scoped=self.cluster_scoped,
                    resource_kinds=[kind],
                )
                self._resource_versions.update(refreshed.resource_versions)
                refreshed_by_key = {
                    _document_key(document): document for document in refreshed.documents
                }
                old_keys = set(self._keys_by_kind.get(kind, set()))
                new_keys = set(refreshed_by_key)
                removed_keys.update(old_keys - new_keys)
                for key in old_keys - new_keys:
                    self._documents_by_key.pop(key, None)
                for key, document in refreshed_by_key.items():
                    if key not in self._documents_by_key or self._documents_by_key[key].raw != document.raw:
                        changed_keys.add(key)
                        changed_documents[key] = document
                    self._documents_by_key[key] = document
                self._keys_by_kind[kind] = new_keys
        else:
            for event in watch_result.events:
                self._apply_watch_event(
                    event,
                    changed_keys=changed_keys,
                    removed_keys=removed_keys,
                    changed_documents=changed_documents,
                )

        if not changed_keys and not removed_keys:
            return None
        return SourceUpdate(
            documents=[changed_documents[key] for key in changed_keys if key in changed_documents],
            changed_keys=changed_keys,
            removed_keys=removed_keys,
            current_keys=set(self._documents_by_key),
        )

    def _apply_watch_event(
        self,
        event: WatchEvent,
        *,
        changed_keys: set[str],
        removed_keys: set[str],
        changed_documents: dict[str, NodeNetworkConfig | NodeNetplanConfig],
    ) -> None:
        if not event.key:
            return
        kind_keys = self._keys_by_kind.setdefault(event.kind, set())
        if event.event_type == "DELETED":
            self._documents_by_key.pop(event.key, None)
            kind_keys.discard(event.key)
            removed_keys.add(event.key)
            return
        if event.document is None:
            return
        previous = self._documents_by_key.get(event.key)
        self._documents_by_key[event.key] = event.document
        kind_keys.add(event.key)
        if previous is None or previous.raw != event.document.raw:
            changed_keys.add(event.key)
            changed_documents[event.key] = event.document


def run_controller(
    *,
    source: DocumentSource,
    state_file: str,
    status_file: str | None = None,
    interval_seconds: float = 30.0,
    once: bool = False,
    max_iterations: int | None = None,
    apply: bool = False,
    vyos_client: VyosApiClient | None = None,
    write_status: bool = False,
    status_writer: KubeStatusWriter | None = None,
    dry_run_status: bool = False,
    cluster_scoped_status: bool = False,
    deleted_retention_seconds: float = 300.0,
) -> ControllerRunResult:
    if apply and vyos_client is None:
        raise ValueError("apply=True requires a VyosApiClient")
    if write_status and status_writer is None:
        raise ValueError("write_status=True requires a KubeStatusWriter")

    result = ControllerRunResult(once=once, interval_seconds=interval_seconds, source=source.name)
    translator = VyosTranslator()
    pending_update = source.initial_update()
    iteration = 0
    while True:
        iteration += 1
        try:
            state = ReconcileState.load(state_file)
            removed_keys = set(pending_update.removed_keys)
            if pending_update.current_keys is not None:
                removed_keys.update(
                    key
                    for key, document in state.documents.items()
                    if not document.deleted and key not in pending_update.current_keys
                )
            deleted_keys = state.mark_deleted(removed_keys)

            if deleted_keys and apply:
                teardown_documents(
                    deleted_keys,
                    state,
                    state_file,
                    client=vyos_client,
                )

            reconcile_result = reconcile_documents(
                pending_update.documents,
                translator,
                state,
                state_file,
                apply=apply,
                client=vyos_client,
                status_file=status_file,
            )

            status_write_result = None
            if write_status:
                report = build_status_report(state)
                status_write_result = status_writer.write_status(
                    report,
                    dry_run=dry_run_status,
                    cluster_scoped=cluster_scoped_status,
                )

            pruned_keys = state.prune_deleted(retention_seconds=deleted_retention_seconds)
            if pruned_keys:
                state.save(state_file)
                reconcile_result.status_report = build_status_report(state).to_dict()
                if status_file:
                    write_status_report(state, status_file)

            result.iterations.append(
                ControllerIterationResult(
                    iteration=iteration,
                    ok=True,
                    changed_documents=len(pending_update.changed_keys) + len(pending_update.removed_keys),
                    deleted_documents=len(deleted_keys),
                    pruned_documents=len(pruned_keys),
                    pending_command_count=reconcile_result.command_count,
                    status_patch_count=(
                        len(status_write_result.patches) if status_write_result else 0
                    ),
                    apply_performed=reconcile_result.apply_performed,
                    status_write_performed=(
                        bool(status_write_result and not status_write_result.dry_run)
                    ),
                    reconcile=reconcile_result.to_dict(),
                    status_write=(
                        status_write_result.to_dict() if status_write_result else None
                    ),
                )
            )
        except Exception as exc:  # pragma: no cover - exercised via CLI smoke paths
            print(f"controller iteration {iteration} failed: {exc}", file=sys.stderr)
            result.iterations.append(
                ControllerIterationResult(
                    iteration=iteration,
                    ok=False,
                    error=str(exc),
                )
            )
            if once:
                return result

        if once:
            return result
        if max_iterations is not None and iteration >= max_iterations:
            return result
        while True:
            try:
                next_update = source.wait_for_update(interval_seconds)
                if next_update is not None:
                    pending_update = next_update
                    break
            except Exception as exc:  # pragma: no cover - exercised via CLI smoke paths
                print(f"controller wait failed after iteration {iteration}: {exc}", file=sys.stderr)
                time.sleep(interval_seconds)


def _file_mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _build_kind_index(
    documents_by_key: dict[str, NodeNetworkConfig | NodeNetplanConfig]
) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for key, document in documents_by_key.items():
        index.setdefault(document.kind, set()).add(key)
    return index
