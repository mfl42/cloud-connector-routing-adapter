from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CustomResourceSpec:
    api_version: str
    kind: str
    plural: str


SUPPORTED_CUSTOM_RESOURCES: list[CustomResourceSpec] = [
    CustomResourceSpec(
        api_version="network.t-caas.telekom.com/v1alpha1",
        kind="NodeNetworkConfig",
        plural="nodenetworkconfigs",
    ),
    CustomResourceSpec(
        api_version="network.t-caas.telekom.com/v1alpha1",
        kind="NodeNetplanConfig",
        plural="nodenetplanconfigs",
    ),
]


def register_resource(spec: CustomResourceSpec) -> None:
    """Register an additional CRD kind at runtime.

    Call this before the first ``list_documents`` or ``watch_for_change``
    invocation to add a new kind to the adapter without modifying this file.
    Registering a kind that is already present (same ``kind`` field) replaces
    the existing entry.
    """
    for i, existing in enumerate(SUPPORTED_CUSTOM_RESOURCES):
        if existing.kind == spec.kind:
            SUPPORTED_CUSTOM_RESOURCES[i] = spec
            return
    SUPPORTED_CUSTOM_RESOURCES.append(spec)


def resolve_resources(resource_kinds: list[str] | None) -> list[CustomResourceSpec]:
    if not resource_kinds:
        return list(SUPPORTED_CUSTOM_RESOURCES)

    indexed = {resource.kind: resource for resource in SUPPORTED_CUSTOM_RESOURCES}
    resolved: list[CustomResourceSpec] = []
    for kind in resource_kinds:
        if kind not in indexed:
            raise ValueError(f"unsupported Kubernetes document kind: {kind}")
        resolved.append(indexed[kind])
    return resolved


def split_api_version(api_version: str) -> tuple[str | None, str]:
    if "/" not in api_version:
        return None, api_version
    group, version = api_version.split("/", 1)
    return group, version


def kind_to_plural(kind: str) -> str:
    for resource in SUPPORTED_CUSTOM_RESOURCES:
        if resource.kind == kind:
            return resource.plural
    raise ValueError(f"unsupported Kubernetes resource kind: {kind!r}")

