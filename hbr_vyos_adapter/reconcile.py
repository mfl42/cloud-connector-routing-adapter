from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime

from .models import NodeNetplanConfig
from .models import NodeNetworkConfig
from .state import DocumentState
from .state import ReconcileState
from .status import build_status_report
from .status import write_status_report
from .translator import TranslationResult
from .translator import VyosTranslator
from .vyos_api import VyosApiClient


@dataclass(slots=True)
class DocumentReconcileResult:
    key: str
    api_version: str
    kind: str
    name: str
    namespace: str | None
    generation: int | None
    resource_version: str | None
    desired_revision: str
    desired_digest: str
    applied_revision: str | None
    applied_digest: str | None
    in_sync: bool
    changed: bool
    command_count: int
    warning_count: int
    unsupported_count: int
    action: str
    warnings: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ReconcileRunResult:
    apply_requested: bool
    apply_performed: bool
    state_file: str
    status_file: str | None
    command_count: int
    documents: list[DocumentReconcileResult] = field(default_factory=list)
    vyos_response: dict | None = None
    status_report: dict | None = None

    def to_dict(self) -> dict:
        return {
            "apply_requested": self.apply_requested,
            "apply_performed": self.apply_performed,
            "state_file": self.state_file,
            "status_file": self.status_file,
            "command_count": self.command_count,
            "documents": [item.to_dict() for item in self.documents],
            "vyos_response": self.vyos_response,
            "status_report": self.status_report,
        }


def reconcile_documents(
    documents: list[NodeNetworkConfig | NodeNetplanConfig],
    translator: VyosTranslator,
    state: ReconcileState,
    state_file: str,
    *,
    apply: bool = False,
    client: VyosApiClient | None = None,
    status_file: str | None = None,
) -> ReconcileRunResult:
    now = _utc_now()
    doc_results: list[DocumentReconcileResult] = []
    pending_commands: list[str] = []
    pending_states: list[tuple[str, str, str, list[str]]] = []

    for document in documents:
        translated = translator.translate(document)
        key = _document_key(document)
        desired_revision = _desired_revision(document, translated.commands)
        desired_digest = _commands_digest(translated.commands)
        existing = state.documents.get(key)
        applied_revision = existing.applied_revision if existing else None
        applied_digest = existing.applied_digest if existing else None
        revisions_changed = applied_revision != desired_revision
        commands_changed = applied_digest != desired_digest
        in_sync = not revisions_changed and not commands_changed
        changed = not in_sync
        action = "noop" if in_sync else ("apply" if apply else "pending-apply")

        state.documents[key] = DocumentState(
            key=key,
            api_version=document.api_version,
            kind=document.kind,
            name=document.metadata.name,
            namespace=document.metadata.namespace,
            generation=document.metadata.generation,
            resource_version=document.metadata.resource_version,
            desired_revision=desired_revision,
            desired_digest=desired_digest,
            applied_revision=applied_revision,
            applied_digest=applied_digest,
            command_count=len(translated.commands),
            warning_count=len(translated.warnings),
            unsupported_count=len(translated.unsupported),
            last_result="in-sync" if in_sync else "pending-apply",
            last_error=None,
            last_seen_at=now,
            last_applied_at=existing.last_applied_at if existing else None,
        )

        if changed:
            if commands_changed:
                old_cmds = set(existing.applied_commands) if existing else set()
                new_cmds = set(translated.commands)
                pending_commands.extend(_compute_diff_deletes(old_cmds - new_cmds, new_cmds))
                pending_commands.extend(translated.commands)
            pending_states.append((key, desired_revision, desired_digest, translated.commands))

        doc_results.append(
            DocumentReconcileResult(
                key=key,
                api_version=document.api_version,
                kind=document.kind,
                name=document.metadata.name,
                namespace=document.metadata.namespace,
                generation=document.metadata.generation,
                resource_version=document.metadata.resource_version,
                desired_revision=desired_revision,
                desired_digest=desired_digest,
                applied_revision=applied_revision,
                applied_digest=applied_digest,
                in_sync=in_sync,
                changed=changed,
                command_count=len(translated.commands),
                warning_count=len(translated.warnings),
                unsupported_count=len(translated.unsupported),
                action=action,
                warnings=translated.warnings,
                unsupported=translated.unsupported,
            )
        )

    apply_performed = False
    vyos_response: dict | None = None
    if apply and pending_states:
        if pending_commands:
            if client is None:
                raise ValueError("apply=True requires a VyosApiClient when commands must be sent")
            vyos_response = client.configure_commands(pending_commands)
            apply_performed = True
        applied_at = _utc_now()
        for key, desired_revision, desired_digest, desired_commands in pending_states:
            entry = state.documents[key]
            entry.applied_revision = desired_revision
            entry.applied_digest = desired_digest
            entry.applied_commands = desired_commands
            entry.last_applied_at = applied_at
            entry.last_result = "applied"
            entry.last_error = None

        for item in doc_results:
            if item.changed:
                item.applied_revision = item.desired_revision
                item.applied_digest = item.desired_digest
                item.in_sync = True
                item.action = "applied"

    state.save(state_file)
    if status_file:
        status_report = write_status_report(state, status_file).to_dict()
    else:
        status_report = build_status_report(state).to_dict()
    return ReconcileRunResult(
        apply_requested=apply,
        apply_performed=apply_performed,
        state_file=state_file,
        status_file=status_file,
        command_count=len(pending_commands),
        documents=doc_results,
        vyos_response=vyos_response,
        status_report=status_report,
    )


def teardown_documents(
    keys: set[str],
    state: ReconcileState,
    state_file: str,
    *,
    client: VyosApiClient | None = None,
) -> list[str]:
    """Generate and optionally apply VyOS delete commands for fully removed documents."""
    teardown_commands: list[str] = []
    for key in sorted(keys):
        doc = state.documents.get(key)
        if doc is None or not doc.applied_commands:
            continue
        teardown_commands.extend(_invert_for_teardown(doc.applied_commands))

    if teardown_commands and client is not None:
        client.configure_commands(teardown_commands)

    torn_down_at = _utc_now()
    for key in sorted(keys):
        doc = state.documents.get(key)
        if doc is None:
            continue
        doc.applied_commands = []
        doc.applied_revision = None
        doc.applied_digest = None
        doc.last_result = "torn-down"
        doc.last_applied_at = torn_down_at

    state.save(state_file)
    return teardown_commands


def _invert_for_teardown(commands: list[str]) -> list[str]:
    """Derive minimal coarse-grained VyOS delete commands from a set of applied set commands.

    VRF blocks, policy route maps, and interface VRF attachments are collapsed into
    single top-level deletes. Everything else (netplan addresses, nameservers, plain
    static routes) gets a fine-grained delete that preserves the original quoting.
    """
    vrf_names: set[str] = set()
    policy_maps: set[str] = set()
    interface_vrfs: set[str] = set()
    fine_deletes: list[str] = []

    for cmd in commands:
        m = re.match(r"^set (vrf name '[^']+')(\s|$)", cmd)
        if m:
            vrf_names.add(m.group(1))
            continue

        m = re.match(r"^set (policy route6? '[^']+')(\s|$)", cmd)
        if m:
            policy_maps.add(m.group(1))
            continue

        m = re.match(r"^set (interfaces \w+ \S+ vif '[^']+') vrf ", cmd)
        if m:
            interface_vrfs.add(m.group(1) + " vrf")
            continue

        m = re.match(r"^set (interfaces \w+ \S+) vrf ", cmd)
        if m:
            interface_vrfs.add(m.group(1) + " vrf")
            continue

        if cmd.startswith("set "):
            fine_deletes.append("delete " + cmd[4:])

    deletes: list[str] = []
    for item in sorted(vrf_names):
        deletes.append(f"delete {item}")
    for item in sorted(policy_maps):
        deletes.append(f"delete {item}")
    for item in sorted(interface_vrfs):
        deletes.append(f"delete {item}")
    deletes.extend(fine_deletes)
    return deletes


def _document_key(document: NodeNetworkConfig | NodeNetplanConfig) -> str:
    namespace = document.metadata.namespace or "default"
    return f"{document.kind}:{namespace}/{document.metadata.name}"


def _desired_revision(
    document: NodeNetworkConfig | NodeNetplanConfig, commands: list[str]
) -> str:
    if isinstance(document, NodeNetworkConfig) and document.revision:
        return document.revision
    return f"digest-{_commands_digest(commands)[:12]}"


def _compute_diff_deletes(removed_cmds: set[str], new_cmds: set[str]) -> list[str]:
    """Generate VyOS delete commands for removed commands.

    BGP neighbors that are entirely removed are collapsed into a single coarse
    ``delete ... neighbor 'addr'`` to avoid leaving VyOS with an incomplete
    neighbor config after a partial leaf delete.
    """
    _bgp_neighbor_re = re.compile(
        r"^set (vrf name '[^']+' protocols bgp neighbor '[^']+')(?:\s|$)"
    )
    neighbor_prefixes: dict[str, list[str]] = {}
    for cmd in removed_cmds:
        m = _bgp_neighbor_re.match(cmd)
        if m:
            neighbor_prefixes.setdefault(m.group(1), []).append(cmd)

    coarse: list[str] = []
    absorbed: set[str] = set()
    for prefix, cmds in sorted(neighbor_prefixes.items()):
        if not any(c.startswith(f"set {prefix}") for c in new_cmds):
            coarse.append(f"delete {prefix}")
            absorbed.update(cmds)

    fine = sorted(
        "delete " + cmd[4:]
        for cmd in removed_cmds - absorbed
        if cmd.startswith("set ")
    )
    return coarse + fine


def _commands_digest(commands: list[str]) -> str:
    payload = "\n".join(commands).encode()
    return hashlib.sha256(payload).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
