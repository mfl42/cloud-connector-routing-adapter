from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile


@dataclass(slots=True)
class DocumentState:
    key: str
    api_version: str
    kind: str
    name: str
    namespace: str | None
    generation: int | None = None
    resource_version: str | None = None
    desired_revision: str | None = None
    desired_digest: str | None = None
    applied_revision: str | None = None
    applied_digest: str | None = None
    command_count: int = 0
    warning_count: int = 0
    unsupported_count: int = 0
    last_result: str = "unknown"
    last_error: str | None = None
    deleted: bool = False
    deleted_at: str | None = None
    last_seen_at: str | None = None
    last_applied_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "DocumentState":
        return cls(
            key=str(data.get("key", "")),
            api_version=str(data.get("api_version", "")),
            kind=str(data.get("kind", "")),
            name=str(data.get("name", "")),
            namespace=data.get("namespace"),
            generation=_int_or_none(data.get("generation")),
            resource_version=data.get("resource_version"),
            desired_revision=data.get("desired_revision"),
            desired_digest=data.get("desired_digest"),
            applied_revision=data.get("applied_revision"),
            applied_digest=data.get("applied_digest"),
            command_count=int(data.get("command_count", 0)),
            warning_count=int(data.get("warning_count", 0)),
            unsupported_count=int(data.get("unsupported_count", 0)),
            last_result=str(data.get("last_result", "unknown")),
            last_error=data.get("last_error"),
            deleted=bool(data.get("deleted", False)),
            deleted_at=data.get("deleted_at"),
            last_seen_at=data.get("last_seen_at"),
            last_applied_at=data.get("last_applied_at"),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ReconcileState:
    documents: dict[str, DocumentState] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "ReconcileState":
        state_path = Path(path)
        if not state_path.exists():
            return cls()

        content = state_path.read_text().strip()
        if not content:
            return cls()

        raw = json.loads(content)
        documents = {
            key: DocumentState.from_dict(value)
            for key, value in (raw.get("documents") or {}).items()
            if isinstance(value, dict)
        }
        return cls(documents=documents)

    def save(self, path: str | Path) -> None:
        state_path = Path(path)
        if state_path.parent != Path("."):
            state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "documents": {
                key: value.to_dict()
                for key, value in sorted(self.documents.items(), key=lambda item: item[0])
            }
        }
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=state_path.parent,
            prefix=f".{state_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            temp_path = Path(handle.name)
        temp_path.replace(state_path)

    def mark_deleted(self, keys: set[str], *, deleted_at: str | None = None) -> set[str]:
        timestamp = deleted_at or _utc_now()
        marked: set[str] = set()
        for key in keys:
            document = self.documents.get(key)
            if document is None:
                continue
            if document.deleted:
                continue
            document.deleted = True
            document.deleted_at = timestamp
            document.last_result = "deleted"
            document.last_error = None
            document.last_seen_at = timestamp
            marked.add(key)
        return marked

    def prune_deleted(self, *, now: str | None = None, retention_seconds: float = 300.0) -> set[str]:
        if retention_seconds < 0:
            return set()
        current = _parse_timestamp(now or _utc_now())
        pruned: set[str] = set()
        for key, document in list(self.documents.items()):
            if not document.deleted or not document.deleted_at:
                continue
            deleted_at = _parse_timestamp(document.deleted_at)
            age_seconds = (current - deleted_at).total_seconds()
            if age_seconds >= retention_seconds:
                self.documents.pop(key, None)
                pruned.add(key)
        return pruned


def _int_or_none(value) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)
