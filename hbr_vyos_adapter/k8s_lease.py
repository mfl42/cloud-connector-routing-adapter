"""Kubernetes Lease-based leader election for the adapter controller.

Uses the ``coordination.k8s.io/v1`` Lease API to ensure only one adapter
instance applies VyOS configuration at a time.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from .k8s_status import KubeConnection


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@dataclass(slots=True)
class LeaseState:
    holder: str | None = None
    renew_time: datetime | None = None
    lease_duration_seconds: int = 15
    resource_version: str | None = None

    @property
    def expired(self) -> bool:
        if self.renew_time is None:
            return True
        deadline = self.renew_time + timedelta(seconds=self.lease_duration_seconds)
        return _utc_now() >= deadline


@dataclass(slots=True)
class LeaseManager:
    """Protocol-compatible base — subclass or use NoopLeaseManager."""

    def acquire(self) -> bool:
        """Try to acquire or renew the lease.  Returns True if this instance is leader."""
        return True

    def release(self) -> None:
        """Release the lease on shutdown."""

    @property
    def is_leader(self) -> bool:
        return True

    @property
    def holder_identity(self) -> str:
        return ""


class NoopLeaseManager(LeaseManager):
    """Always-leader — used when ``--enable-leader-election`` is not set."""

    pass


@dataclass(slots=True)
class KubeLeaseManager(LeaseManager):
    """Kubernetes Lease-based leader election.

    Lease object: ``coordination.k8s.io/v1`` Lease in *lease_namespace*
    with name *lease_name*.
    """

    connection: KubeConnection
    lease_name: str = "cloud-connector-routing-adapter"
    lease_namespace: str = "default"
    lease_duration_seconds: int = 15
    leader_id: str = ""
    timeout: int = 10
    _state: LeaseState | None = None
    _is_leader: bool = False

    def __post_init__(self) -> None:
        if not self.leader_id:
            self.leader_id = os.environ.get("HOSTNAME", "") or os.environ.get(
                "POD_NAME", ""
            ) or f"adapter-{os.getpid()}"

    # -- public API -----------------------------------------------------------

    def acquire(self) -> bool:
        try:
            self._state = self._read_lease()
        except _LeaseNotFound:
            self._state = self._create_lease()
            self._is_leader = True
            return True
        except Exception:
            self._is_leader = False
            return False

        if self._state.holder == self.leader_id:
            # We already hold the lease — renew.
            return self._renew()

        if self._state.expired:
            # Previous holder lost it — take over.
            return self._take_over()

        # Another instance holds an active lease.
        self._is_leader = False
        return False

    def release(self) -> None:
        if not self._is_leader or self._state is None:
            return
        try:
            self._update_lease(holder=None, renew_time=None)
        except Exception:
            pass
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    @property
    def holder_identity(self) -> str:
        return self.leader_id

    # -- internals ------------------------------------------------------------

    def _lease_url(self) -> str:
        server = self.connection.server.rstrip("/")
        return (
            f"{server}/apis/coordination.k8s.io/v1"
            f"/namespaces/{self.lease_namespace}/leases/{self.lease_name}"
        )

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.connection.token:
            headers["Authorization"] = f"Bearer {self.connection.token}"
        return headers

    def _http_kwargs(self) -> dict:
        return {
            "headers": self._headers(),
            "timeout": self.timeout,
            "verify": self.connection.verify_value(),
            "cert": self.connection.cert_value(),
        }

    def _read_lease(self) -> LeaseState:
        import requests

        response = requests.get(self._lease_url(), **self._http_kwargs())
        if response.status_code == 404:
            raise _LeaseNotFound()
        if response.status_code >= 400:
            raise RuntimeError(
                f"Lease read failed: HTTP {response.status_code}"
            )
        return _parse_lease(response.json())

    def _create_lease(self) -> LeaseState:
        import requests

        now = _utc_now()
        body = _lease_body(
            name=self.lease_name,
            namespace=self.lease_namespace,
            holder=self.leader_id,
            duration=self.lease_duration_seconds,
            renew_time=now,
        )
        response = requests.post(
            self._lease_url().rsplit("/", 1)[0],
            data=json.dumps(body),
            **self._http_kwargs(),
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Lease create failed: HTTP {response.status_code}"
            )
        return _parse_lease(response.json())

    def _update_lease(
        self,
        holder: str | None,
        renew_time: datetime | None,
    ) -> LeaseState:
        import requests

        body = {
            "metadata": {
                "resourceVersion": self._state.resource_version if self._state else None,
            },
            "spec": {
                "holderIdentity": holder,
                "leaseDurationSeconds": self.lease_duration_seconds,
                "renewTime": renew_time.isoformat() if renew_time else None,
            },
        }
        response = requests.put(
            self._lease_url(),
            data=json.dumps(body),
            **self._http_kwargs(),
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Lease update failed: HTTP {response.status_code}"
            )
        return _parse_lease(response.json())

    def _renew(self) -> bool:
        try:
            self._state = self._update_lease(
                holder=self.leader_id,
                renew_time=_utc_now(),
            )
            self._is_leader = True
            return True
        except Exception:
            self._is_leader = False
            return False

    def _take_over(self) -> bool:
        try:
            self._state = self._update_lease(
                holder=self.leader_id,
                renew_time=_utc_now(),
            )
            self._is_leader = True
            return True
        except Exception:
            self._is_leader = False
            return False


class _LeaseNotFound(Exception):
    pass


def _parse_lease(data: dict) -> LeaseState:
    spec = data.get("spec") or {}
    renew_raw = spec.get("renewTime")
    renew_time = None
    if renew_raw:
        try:
            renew_time = datetime.fromisoformat(renew_raw)
        except (ValueError, TypeError):
            pass
    return LeaseState(
        holder=spec.get("holderIdentity"),
        renew_time=renew_time,
        lease_duration_seconds=spec.get("leaseDurationSeconds") or 15,
        resource_version=(data.get("metadata") or {}).get("resourceVersion"),
    )


def _lease_body(
    *,
    name: str,
    namespace: str,
    holder: str,
    duration: int,
    renew_time: datetime,
) -> dict:
    return {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "holderIdentity": holder,
            "leaseDurationSeconds": duration,
            "renewTime": renew_time.isoformat(),
        },
    }
