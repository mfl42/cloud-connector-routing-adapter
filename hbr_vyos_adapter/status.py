from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from .state import DocumentState
from .state import ReconcileState


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class StatusCondition:
    type: str
    status: str
    reason: str
    message: str
    last_transition_time: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class DocumentStatusReport:
    key: str
    api_version: str
    kind: str
    name: str
    namespace: str | None
    generation: int | None
    resource_version: str | None
    phase: str
    desired_revision: str | None
    applied_revision: str | None
    desired_digest: str | None
    applied_digest: str | None
    command_count: int
    warning_count: int
    unsupported_count: int
    last_result: str
    last_error: str | None
    deleted: bool
    deleted_at: str | None
    last_seen_at: str | None
    last_applied_at: str | None
    conditions: list[StatusCondition] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "conditions": [condition.to_dict() for condition in self.conditions],
        }


@dataclass(slots=True)
class StatusReport:
    api_version: str = "adapter.mfl42.io/v1alpha1"
    kind: str = "AdapterStatusReport"
    generated_at: str = field(default_factory=_utc_now)
    document_count: int = 0
    documents: list[DocumentStatusReport] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "apiVersion": self.api_version,
            "kind": self.kind,
            "generatedAt": self.generated_at,
            "documentCount": self.document_count,
            "documents": [item.to_dict() for item in self.documents],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def build_status_report(state: ReconcileState) -> StatusReport:
    documents = [_build_document_status(item) for item in _sorted_documents(state)]
    return StatusReport(document_count=len(documents), documents=documents)


def write_status_report(state: ReconcileState, path: str | Path) -> StatusReport:
    report = build_status_report(state)
    output_path = Path(path)
    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(report.to_json() + "\n")
        temp_path = Path(handle.name)
    temp_path.replace(output_path)
    return report


def _build_document_status(document: DocumentState) -> DocumentStatusReport:
    phase = _phase(document)
    transition_time = document.last_applied_at or document.last_seen_at or _utc_now()
    desired_seen = StatusCondition(
        type="DesiredSeen",
        status="True" if document.desired_revision or document.desired_digest else "False",
        reason="DesiredStateRecorded",
        message="The adapter has recorded a desired revision and command digest.",
        last_transition_time=document.last_seen_at or transition_time,
    )
    in_sync = _in_sync(document)
    sync_condition = StatusCondition(
        type="InSync",
        status="True" if in_sync else "False",
        reason="StateSynchronized" if in_sync else "PendingApply",
        message=(
            "Desired and applied state digests match."
            if in_sync
            else "Desired and applied state are different."
        ),
        last_transition_time=transition_time,
    )
    applied_condition = StatusCondition(
        type="Applied",
        status="True" if document.applied_revision or document.applied_digest else "False",
        reason="ApplyRecorded" if document.applied_revision or document.applied_digest else "NeverApplied",
        message=(
            "The adapter has recorded an applied revision and digest."
            if document.applied_revision or document.applied_digest
            else "No applied revision has been recorded yet."
        ),
        last_transition_time=document.last_applied_at or transition_time,
    )

    conditions = [desired_seen, applied_condition, sync_condition]
    if document.deleted:
        conditions.append(
            StatusCondition(
                type="Deleted",
                status="True",
                reason="SourceRemovalDetected",
                message="The adapter observed this document being removed from the source.",
                last_transition_time=document.deleted_at or transition_time,
            )
        )
    if document.warning_count:
        conditions.append(
            StatusCondition(
                type="HasWarnings",
                status="True",
                reason="TranslationWarningsPresent",
                message=f"{document.warning_count} translation warning(s) were recorded.",
                last_transition_time=document.last_seen_at or transition_time,
            )
        )
    if document.unsupported_count:
        conditions.append(
            StatusCondition(
                type="HasUnsupported",
                status="True",
                reason="UnsupportedSemanticsPresent",
                message=f"{document.unsupported_count} unsupported item(s) were recorded.",
                last_transition_time=document.last_seen_at or transition_time,
            )
        )
    if document.last_error:
        conditions.append(
            StatusCondition(
                type="Error",
                status="True",
                reason="LastOperationFailed",
                message=document.last_error,
                last_transition_time=document.last_seen_at or transition_time,
            )
        )

    return DocumentStatusReport(
        key=document.key,
        api_version=document.api_version,
        kind=document.kind,
        name=document.name,
        namespace=document.namespace,
        generation=document.generation,
        resource_version=document.resource_version,
        phase=phase,
        desired_revision=document.desired_revision,
        applied_revision=document.applied_revision,
        desired_digest=document.desired_digest,
        applied_digest=document.applied_digest,
        command_count=document.command_count,
        warning_count=document.warning_count,
        unsupported_count=document.unsupported_count,
        last_result=document.last_result,
        last_error=document.last_error,
        deleted=document.deleted,
        deleted_at=document.deleted_at,
        last_seen_at=document.last_seen_at,
        last_applied_at=document.last_applied_at,
        conditions=conditions,
    )


def _sorted_documents(state: ReconcileState) -> list[DocumentState]:
    return [item[1] for item in sorted(state.documents.items(), key=lambda value: value[0])]


def _in_sync(document: DocumentState) -> bool:
    if document.deleted:
        return False
    return (
        bool(document.desired_revision or document.desired_digest)
        and document.desired_revision == document.applied_revision
        and document.desired_digest == document.applied_digest
    )


def _phase(document: DocumentState) -> str:
    if document.deleted:
        return "Deleted"
    if document.last_error:
        return "Error"
    if _in_sync(document):
        return "InSync"
    if document.applied_revision or document.applied_digest:
        return "Drifted"
    return "PendingApply"
