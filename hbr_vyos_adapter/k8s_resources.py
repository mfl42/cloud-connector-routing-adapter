from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hbr_vyos_adapter.models import DocumentFactory


@dataclass(frozen=True, slots=True)
class CustomResourceSpec:
    api_version: str
    kind: str
    plural: str


SUPPORTED_CUSTOM_RESOURCES: list[CustomResourceSpec] = [
    # t-caas.telekom.com — v1alpha1 (current production API)
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

# Known alternative API groups — not watched by default but parseable via
# ModelRegistry and activatable at runtime with register_resource().
KNOWN_API_VARIANTS: list[CustomResourceSpec] = [
    # t-caas.telekom.com — v1beta1 (forward-compatible alias)
    CustomResourceSpec(
        api_version="network.t-caas.telekom.com/v1beta1",
        kind="NodeNetworkConfig",
        plural="nodenetworkconfigs",
    ),
    CustomResourceSpec(
        api_version="network.t-caas.telekom.com/v1beta1",
        kind="NodeNetplanConfig",
        plural="nodenetplanconfigs",
    ),
    # Sylva project — sylva.io/v1alpha1 (upstream Sylva CRDs)
    # Activate with: register_resource(KNOWN_API_VARIANTS[2])
    CustomResourceSpec(
        api_version="sylva.io/v1alpha1",
        kind="NodeNetworkConfig",
        plural="nodenetworkconfigs",
    ),
    CustomResourceSpec(
        api_version="sylva.io/v1alpha1",
        kind="NodeNetplanConfig",
        plural="nodenetplanconfigs",
    ),
]


def register_resource(
    spec: CustomResourceSpec,
    factory: "DocumentFactory | None" = None,
) -> None:
    """Register an additional CRD kind at runtime.

    Call this before the first ``list_documents`` or ``watch_for_change``
    invocation to add a new kind to the adapter without modifying this file.

    ``factory`` is an optional document factory function
    (``dict → NodeNetworkConfig | NodeNetplanConfig``).  When provided it is
    also registered in the global ``ModelRegistry`` so that ``load_document``
    can parse documents of the new kind.  Omit it to reuse the existing parser
    for the same ``kind`` (fallback lookup in ``ModelRegistry``).

    Registering a ``(api_version, kind)`` pair that is already present replaces
    the existing entry.
    """
    for i, existing in enumerate(SUPPORTED_CUSTOM_RESOURCES):
        if existing.api_version == spec.api_version and existing.kind == spec.kind:
            SUPPORTED_CUSTOM_RESOURCES[i] = spec
            break
    else:
        SUPPORTED_CUSTOM_RESOURCES.append(spec)

    if factory is not None:
        from hbr_vyos_adapter.models import register_model

        register_model(spec.api_version, spec.kind, factory)


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

