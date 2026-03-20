from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .k8s_resources import kind_to_plural
from .k8s_resources import split_api_version
from .status import DocumentStatusReport
from .status import StatusReport


@dataclass(slots=True)
class StatusPatchPlan:
    key: str
    api_version: str
    kind: str
    namespace: str | None
    name: str
    plural: str
    url: str
    body: dict[str, Any]
    cluster_scoped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "apiVersion": self.api_version,
            "kind": self.kind,
            "namespace": self.namespace,
            "name": self.name,
            "plural": self.plural,
            "url": self.url,
            "body": self.body,
            "clusterScoped": self.cluster_scoped,
        }


@dataclass(slots=True)
class StatusWriteResult:
    dry_run: bool
    patches: list[StatusPatchPlan] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    responses: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "patches": [patch.to_dict() for patch in self.patches],
            "skipped": self.skipped,
            "responses": self.responses,
        }


@dataclass(slots=True)
class KubeConnection:
    server: str
    token: str | None = None
    ca_cert: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    verify_tls: bool = True

    def verify_value(self) -> bool | str:
        if self.ca_cert:
            return self.ca_cert
        if not self.verify_tls:
            return False
        return True

    def cert_value(self) -> str | tuple[str, str] | None:
        if self.client_cert and self.client_key:
            return (self.client_cert, self.client_key)
        if self.client_cert:
            return self.client_cert
        return None


@dataclass(slots=True)
class KubeStatusWriter:
    connection: KubeConnection
    timeout: int = 30
    retry_attempts: int = 3
    retry_backoff_seconds: float = 0.2
    retry_status_codes: tuple[int, ...] = (409, 429, 500, 502, 503, 504)

    def write_status(
        self,
        report: StatusReport,
        *,
        dry_run: bool = False,
        selector: dict[str, str | None] | None = None,
        cluster_scoped: bool = False,
        include_deleted: bool = False,
    ) -> StatusWriteResult:
        patches: list[StatusPatchPlan] = []
        skipped: list[dict[str, str]] = []
        for document in _selected_documents(report, selector):
            if document.deleted and not include_deleted:
                skipped.append(
                    {
                        "key": document.key,
                        "reason": "deleted-document-local-status-only",
                    }
                )
                continue
            patches.append(self._patch_plan(document, cluster_scoped=cluster_scoped))
        if dry_run:
            return StatusWriteResult(dry_run=True, patches=patches, skipped=skipped, responses=[])

        responses: list[dict[str, Any]] = []
        verify = self.connection.verify_value()
        cert = self.connection.cert_value()

        import requests

        headers = {"Content-Type": "application/merge-patch+json"}
        if self.connection.token:
            headers["Authorization"] = f"Bearer {self.connection.token}"

        for patch in patches:
            responses.append(
                self._patch_with_retries(
                    requests,
                    patch=patch,
                    headers=headers,
                    verify=verify,
                    cert=cert,
                )
            )

        return StatusWriteResult(dry_run=False, patches=patches, skipped=skipped, responses=responses)

    def _patch_plan(
        self, document: DocumentStatusReport, *, cluster_scoped: bool = False
    ) -> StatusPatchPlan:
        group, version = split_api_version(document.api_version)
        plural = kind_to_plural(document.kind)
        namespace = document.namespace or "default"
        if group:
            base = f"{self.connection.server.rstrip('/')}/apis/{group}/{version}"
        else:
            base = f"{self.connection.server.rstrip('/')}/api/{version}"

        if cluster_scoped:
            url = f"{base}/{plural}/{document.name}/status"
        else:
            url = f"{base}/namespaces/{namespace}/{plural}/{document.name}/status"
        return StatusPatchPlan(
            key=document.key,
            api_version=document.api_version,
            kind=document.kind,
            namespace=namespace,
            name=document.name,
            plural=plural,
            url=url,
            body=_patch_body(document),
            cluster_scoped=cluster_scoped,
        )

    def _patch_with_retries(
        self,
        requests_module,
        *,
        patch: StatusPatchPlan,
        headers: dict[str, str],
        verify: bool | str,
        cert: str | tuple[str, str] | None,
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            attempt += 1
            try:
                kwargs = {
                    "headers": headers,
                    "data": json.dumps(patch.body),
                    "timeout": self.timeout,
                    "verify": verify,
                }
                if cert is not None:
                    kwargs["cert"] = cert
                response = requests_module.patch(patch.url, **kwargs)
            except Exception as exc:
                if attempt >= self.retry_attempts:
                    raise RuntimeError(
                        f"Kubernetes status patch failed for {patch.key}: {exc}"
                    ) from exc
                time.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))
                continue

            status_code = getattr(response, "status_code", 200)
            if status_code < 400:
                return _response_payload(response)
            if status_code in self.retry_status_codes and attempt < self.retry_attempts:
                time.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))
                continue
            _raise_patch_error(response, patch.key)


def load_kube_connection(
    *,
    kubeconfig: str | None = None,
    context: str | None = None,
    server: str | None = None,
    token: str | None = None,
    verify_tls: bool = True,
) -> KubeConnection:
    if server:
        return KubeConnection(server=server, token=token, verify_tls=verify_tls)

    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "kubeconfig loading requires PyYAML. Install dependencies from "
            "pyproject.toml or pass --server/--token directly."
        ) from exc

    config_path = Path(kubeconfig).expanduser() if kubeconfig else Path("~/.kube/config").expanduser()
    raw = yaml.safe_load(config_path.read_text())
    current_context = context or raw.get("current-context")
    if not current_context:
        raise ValueError("kubeconfig has no current-context and no --context was provided")

    context_entry = _lookup_named(raw.get("contexts") or [], current_context)
    cluster_name = context_entry["context"]["cluster"]
    user_name = context_entry["context"]["user"]

    cluster_entry = _lookup_named(raw.get("clusters") or [], cluster_name)["cluster"]
    user_entry = _lookup_named(raw.get("users") or [], user_name)["user"]

    ca_cert = None
    if "certificate-authority" in cluster_entry:
        ca_cert = str(Path(cluster_entry["certificate-authority"]).expanduser())
    elif "certificate-authority-data" in cluster_entry:
        ca_cert = _materialize_temp_file(base64.b64decode(cluster_entry["certificate-authority-data"]))

    client_cert = None
    if "client-certificate" in user_entry:
        client_cert = str(Path(user_entry["client-certificate"]).expanduser())
    elif "client-certificate-data" in user_entry:
        client_cert = _materialize_temp_file(base64.b64decode(user_entry["client-certificate-data"]))

    client_key = None
    if "client-key" in user_entry:
        client_key = str(Path(user_entry["client-key"]).expanduser())
    elif "client-key-data" in user_entry:
        client_key = _materialize_temp_file(base64.b64decode(user_entry["client-key-data"]))

    resolved_token = token
    if resolved_token is None:
        resolved_token = user_entry.get("token")
    if resolved_token is None and "tokenFile" in user_entry:
        resolved_token = Path(user_entry["tokenFile"]).expanduser().read_text().strip()

    cluster_verify_tls = verify_tls and not cluster_entry.get("insecure-skip-tls-verify", False)
    return KubeConnection(
        server=cluster_entry["server"],
        token=resolved_token,
        ca_cert=ca_cert,
        client_cert=client_cert,
        client_key=client_key,
        verify_tls=cluster_verify_tls,
    )


def _selected_documents(
    report: StatusReport, selector: dict[str, str | None] | None
) -> list[DocumentStatusReport]:
    if not selector:
        return report.documents

    selected: list[DocumentStatusReport] = []
    for document in report.documents:
        if selector.get("key") and selector["key"] != document.key:
            continue
        if selector.get("kind") and selector["kind"] != document.kind:
            continue
        if selector.get("name") and selector["name"] != document.name:
            continue
        if selector.get("namespace") and selector["namespace"] != (document.namespace or "default"):
            continue
        selected.append(document)
    return selected


def _status_body(document: DocumentStatusReport) -> dict[str, Any]:
    body = {
        "phase": document.phase,
        "observedRevision": document.desired_revision,
        "observedGeneration": document.generation,
        "appliedRevision": document.applied_revision,
        "desiredDigest": document.desired_digest,
        "appliedDigest": document.applied_digest,
        "commandCount": document.command_count,
        "warningCount": document.warning_count,
        "unsupportedCount": document.unsupported_count,
        "lastResult": document.last_result,
        "lastError": document.last_error,
        "lastSeenAt": document.last_seen_at,
        "lastAppliedAt": document.last_applied_at,
        "conditions": [
            {
                "type": condition.type,
                "status": condition.status,
                "reason": condition.reason,
                "message": condition.message,
                "lastTransitionTime": condition.last_transition_time,
            }
            for condition in document.conditions
        ],
    }
    return body


def _patch_body(document: DocumentStatusReport) -> dict[str, Any]:
    body: dict[str, Any] = {"status": _status_body(document)}
    if document.resource_version:
        body["metadata"] = {"resourceVersion": document.resource_version}
    return body


def _lookup_named(items: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for item in items:
        if item.get("name") == name:
            return item
    raise ValueError(f"unable to find kubeconfig entry {name!r}")


def _materialize_temp_file(content: bytes) -> str:
    handle = NamedTemporaryFile(delete=False)
    handle.write(content)
    handle.flush()
    handle.close()
    return handle.name


def _response_payload(response) -> dict[str, Any]:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        return {"payload": payload}
    except Exception:
        return {"text": getattr(response, "text", "")}


def _raise_patch_error(response, key: str) -> None:
    payload = _response_payload(response)
    raise RuntimeError(
        f"Kubernetes status patch failed for {key} with HTTP "
        f"{getattr(response, 'status_code', 'unknown')}: {json.dumps(payload)}"
    )
