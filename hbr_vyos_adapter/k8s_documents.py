from __future__ import annotations

import json
import time
from dataclasses import dataclass
from dataclasses import field

from .k8s_resources import CustomResourceSpec
from .k8s_resources import resolve_resources
from .k8s_resources import split_api_version
from .k8s_status import KubeConnection
from .models import NodeNetplanConfig
from .models import NodeNetworkConfig
from .models import load_document


@dataclass(slots=True)
class DocumentSnapshot:
    documents: list[NodeNetworkConfig | NodeNetplanConfig]
    resource_versions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class WatchResult:
    changed: bool
    relist_required: bool = False
    resource_versions: dict[str, str] = field(default_factory=dict)
    events: list["WatchEvent"] = field(default_factory=list)


@dataclass(slots=True)
class WatchEvent:
    kind: str
    event_type: str
    key: str | None = None
    document: NodeNetworkConfig | NodeNetplanConfig | None = None
    relist_required: bool = False


@dataclass(slots=True)
class KubeDocumentClient:
    connection: KubeConnection
    timeout: int = 30
    watch_retry_attempts: int = 3
    watch_retry_backoff_seconds: float = 0.2

    def list_documents(
        self,
        *,
        namespace: str | None = None,
        cluster_scoped: bool = False,
        resource_kinds: list[str] | None = None,
    ) -> DocumentSnapshot:
        documents: list[NodeNetworkConfig | NodeNetplanConfig] = []
        resource_versions: dict[str, str] = {}
        for resource in resolve_resources(resource_kinds):
            payload = self._get_json(
                self._resource_url(resource, namespace=namespace, cluster_scoped=cluster_scoped)
            )
            resource_versions[resource.kind] = str(
                (payload.get("metadata") or {}).get("resourceVersion") or ""
            )
            for item in payload.get("items") or []:
                if isinstance(item, dict) and item.get("kind") == resource.kind:
                    documents.append(load_document(item))
        return DocumentSnapshot(documents=documents, resource_versions=resource_versions)

    def watch_for_change(
        self,
        resource_versions: dict[str, str],
        *,
        namespace: str | None = None,
        cluster_scoped: bool = False,
        resource_kinds: list[str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> WatchResult:
        resources = resolve_resources(resource_kinds)
        if not resources:
            return WatchResult(changed=False, resource_versions=dict(resource_versions))

        per_resource_timeout = max(1, int(timeout_seconds / len(resources)))
        latest_versions = dict(resource_versions)
        for resource in resources:
            changed, latest_version, relist_required, event = self._watch_resource(
                resource,
                resource_version=latest_versions.get(resource.kind),
                namespace=namespace,
                cluster_scoped=cluster_scoped,
                timeout_seconds=per_resource_timeout,
            )
            if latest_version is not None:
                latest_versions[resource.kind] = latest_version
            elif relist_required:
                latest_versions[resource.kind] = ""
            if relist_required:
                return WatchResult(
                    changed=True,
                    relist_required=True,
                    resource_versions=latest_versions,
                    events=[event] if event else [],
                )
            if changed:
                return WatchResult(
                    changed=True,
                    resource_versions=latest_versions,
                    events=[event] if event else [],
                )

        return WatchResult(changed=False, resource_versions=latest_versions)

    def _watch_resource(
        self,
        resource: CustomResourceSpec,
        *,
        resource_version: str | None,
        namespace: str | None,
        cluster_scoped: bool,
        timeout_seconds: int,
    ) -> tuple[bool, str | None, bool, WatchEvent | None]:
        import requests

        attempt = 0
        while True:
            attempt += 1
            try:
                kwargs = {
                    "headers": self._headers(),
                    "params": {
                        "watch": "true",
                        "allowWatchBookmarks": "true",
                        "timeoutSeconds": str(timeout_seconds),
                        "resourceVersion": resource_version or "",
                    },
                    "timeout": self.timeout + timeout_seconds,
                    "verify": self._verify_value(),
                    "stream": True,
                }
                cert = self._cert_value()
                if cert is not None:
                    kwargs["cert"] = cert
                response = requests.get(
                    self._resource_url(resource, namespace=namespace, cluster_scoped=cluster_scoped),
                    **kwargs,
                )
            except Exception:
                if attempt >= self.watch_retry_attempts:
                    raise
                time.sleep(self.watch_retry_backoff_seconds * (2 ** (attempt - 1)))
                continue

            status_code = getattr(response, "status_code", 200)
            if status_code == 410:
                return True, None, True, WatchEvent(
                    kind=resource.kind,
                    event_type="ERROR",
                    relist_required=True,
                )
            if status_code >= 400:
                if attempt >= self.watch_retry_attempts:
                    _raise_http_error(response, resource.kind)
                time.sleep(self.watch_retry_backoff_seconds * (2 ** (attempt - 1)))
                continue
            break

        latest_version = resource_version
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            payload = json.loads(line)
            event_type = payload.get("type")
            if event_type == "ERROR":
                if _is_stale_watch_event(payload):
                    return True, None, True, WatchEvent(
                        kind=resource.kind,
                        event_type="ERROR",
                        relist_required=True,
                    )
                raise RuntimeError(
                    f"Kubernetes watch error for {resource.kind}: {json.dumps(payload)}"
                )

            obj = payload.get("object") or {}
            metadata = obj.get("metadata") or {}
            latest_version = str(metadata.get("resourceVersion") or latest_version or "")
            if event_type in {"ADDED", "MODIFIED", "DELETED"}:
                key = _document_key_from_raw(resource.kind, obj)
                document = None
                if event_type != "DELETED":
                    document = load_document(obj)
                return True, latest_version, False, WatchEvent(
                    kind=resource.kind,
                    event_type=event_type,
                    key=key,
                    document=document,
                )

        return False, latest_version, False, None

    def _get_json(self, url: str) -> dict:
        import requests

        kwargs = {
            "headers": self._headers(),
            "timeout": self.timeout,
            "verify": self._verify_value(),
        }
        cert = self._cert_value()
        if cert is not None:
            kwargs["cert"] = cert
        response = requests.get(url, **kwargs)
        if getattr(response, "status_code", 200) >= 400:
            _raise_http_error(response, "document-list")
        return response.json()

    def _resource_url(
        self,
        resource: CustomResourceSpec,
        *,
        namespace: str | None,
        cluster_scoped: bool,
    ) -> str:
        group, version = split_api_version(resource.api_version)
        if group:
            base = f"{self.connection.server.rstrip('/')}/apis/{group}/{version}"
        else:
            base = f"{self.connection.server.rstrip('/')}/api/{version}"

        if cluster_scoped or namespace is None:
            return f"{base}/{resource.plural}"
        return f"{base}/namespaces/{namespace}/{resource.plural}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.connection.token:
            headers["Authorization"] = f"Bearer {self.connection.token}"
        return headers

    def _verify_value(self) -> bool | str:
        return self.connection.verify_value()

    def _cert_value(self) -> str | tuple[str, str] | None:
        return self.connection.cert_value()


def _is_stale_watch_event(payload: dict) -> bool:
    obj = payload.get("object") or {}
    code = obj.get("code")
    reason = str(obj.get("reason") or "").lower()
    return code == 410 or reason in {
        "expired",
        "gone",
        "resourceexpired",
        "toooldresourceversion",
    }


def _raise_http_error(response, resource_kind: str) -> None:
    body = ""
    try:
        body = json.dumps(response.json())
    except Exception:
        body = getattr(response, "text", "")
    raise RuntimeError(
        f"Kubernetes request for {resource_kind} failed with HTTP "
        f"{getattr(response, 'status_code', 'unknown')}: {body}"
    )


def _document_key_from_raw(kind: str, data: dict) -> str | None:
    metadata = data.get("metadata") or {}
    name = metadata.get("name")
    if not name:
        return None
    namespace = metadata.get("namespace") or "default"
    return f"{kind}:{namespace}/{name}"
