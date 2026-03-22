from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
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

        full_timeout = max(1, int(timeout_seconds))
        latest_versions = dict(resource_versions)

        if len(resources) == 1:
            resource = resources[0]
            changed, latest_version, relist_required, events = self._watch_resource(
                resource,
                resource_version=latest_versions.get(resource.kind),
                namespace=namespace,
                cluster_scoped=cluster_scoped,
                timeout_seconds=full_timeout,
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
                    events=events,
                )
            if changed:
                return WatchResult(
                    changed=True,
                    resource_versions=latest_versions,
                    events=events,
                )
            return WatchResult(changed=False, resource_versions=latest_versions)

        with ThreadPoolExecutor(max_workers=len(resources)) as pool:
            future_to_resource = {
                pool.submit(
                    self._watch_resource,
                    resource,
                    resource_version=latest_versions.get(resource.kind),
                    namespace=namespace,
                    cluster_scoped=cluster_scoped,
                    timeout_seconds=full_timeout,
                ): resource
                for resource in resources
            }
            done, pending = futures_wait(
                future_to_resource,
                timeout=full_timeout + self.timeout,
            )
            for f in pending:
                f.cancel()

        all_events: list[WatchEvent] = []
        any_changed = False
        relist_found = False
        for future in done:
            resource = future_to_resource[future]
            changed, latest_version, relist_required, events = future.result()
            if latest_version is not None:
                latest_versions[resource.kind] = latest_version
            elif relist_required:
                latest_versions[resource.kind] = ""
            all_events.extend(events)
            if relist_required:
                relist_found = True
            if changed:
                any_changed = True

        if relist_found:
            return WatchResult(
                changed=True,
                relist_required=True,
                resource_versions=latest_versions,
                events=all_events,
            )
        if any_changed:
            return WatchResult(
                changed=True,
                resource_versions=latest_versions,
                events=all_events,
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
    ) -> tuple[bool, str | None, bool, list[WatchEvent]]:
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
                return True, None, True, [WatchEvent(
                    kind=resource.kind,
                    event_type="ERROR",
                    relist_required=True,
                )]
            if status_code >= 400:
                if attempt >= self.watch_retry_attempts:
                    _raise_http_error(response, resource.kind)
                time.sleep(self.watch_retry_backoff_seconds * (2 ** (attempt - 1)))
                continue
            break

        # After the first change event is seen, close the stream after a short
        # drain window so that events arriving in rapid succession are all
        # captured in one watch cycle without blocking for the full timeout.
        _DRAIN_DELAY_SECONDS = 0.15
        drain_fired = threading.Event()
        drain_timer: threading.Timer | None = None

        def _fire_drain() -> None:
            drain_fired.set()
            try:
                response.close()
            except Exception:
                pass

        latest_version = resource_version
        events: list[WatchEvent] = []
        try:
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = payload.get("type")
                if event_type == "ERROR":
                    if drain_timer is not None:
                        drain_timer.cancel()
                    if _is_stale_watch_event(payload):
                        return True, None, True, [WatchEvent(
                            kind=resource.kind,
                            event_type="ERROR",
                            relist_required=True,
                        )]
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
                    events.append(WatchEvent(
                        kind=resource.kind,
                        event_type=event_type,
                        key=key,
                        document=document,
                    ))
                    if drain_timer is None:
                        drain_timer = threading.Timer(_DRAIN_DELAY_SECONDS, _fire_drain)
                        drain_timer.daemon = True
                        drain_timer.start()
        except Exception:
            if not drain_fired.is_set():
                raise
            # drain timer closed the stream — normal termination
        finally:
            if drain_timer is not None:
                drain_timer.cancel()

        return bool(events), latest_version, False, events

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
