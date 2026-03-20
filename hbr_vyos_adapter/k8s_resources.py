from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CustomResourceSpec:
    api_version: str
    kind: str
    plural: str


SUPPORTED_CUSTOM_RESOURCES: tuple[CustomResourceSpec, ...] = (
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
)


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

