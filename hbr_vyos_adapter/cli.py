from __future__ import annotations

import argparse
import json
import sys

from .controller import FileDocumentSource
from .controller import KubernetesDocumentSource
from .controller import run_controller
from .k8s_documents import KubeDocumentClient
from .k8s_lease import KubeLeaseManager
from .k8s_lease import LeaseManager
from .k8s_lease import NoopLeaseManager
from .k8s_status import load_kube_connection
from .k8s_status import KubeStatusWriter
from .loader import load_documents
from .reconcile import reconcile_documents
from .state import ReconcileState
from .status import build_status_report
from .translator import TranslationResult
from .translator import VyosTranslator
from .vyos_api import VyosApiClient


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan or apply Cloud Connector routing translation for the current VyOS-compatible profile"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Translate documents into VyOS commands")
    plan_parser.add_argument("--file", required=True, help="Path to YAML or JSON document")
    plan_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")

    reconcile_parser = subparsers.add_parser(
        "reconcile", help="Compare desired state with local adapter state and optionally apply"
    )
    reconcile_parser.add_argument("--file", required=True, help="Path to YAML or JSON document")
    reconcile_parser.add_argument(
        "--state-file",
        default=".cloud-connector-routing-adapter-state.json",
        help="Path to the local reconcile state file",
    )
    reconcile_parser.add_argument(
        "--status-file",
        help="Optional path for a CRD-style status export JSON report",
    )
    reconcile_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    reconcile_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changed commands to VyOS and update last-applied revision state",
    )
    reconcile_parser.add_argument("--vyos-url", help="VyOS API base URL")
    reconcile_parser.add_argument("--api-key", help="VyOS API key")
    reconcile_parser.add_argument(
        "--vyos-timeout",
        type=float,
        default=30.0,
        help="VyOS API timeout in seconds",
    )
    reconcile_parser.add_argument(
        "--verify-tls",
        action="store_true",
        help="Verify TLS certificates when talking to VyOS",
    )

    status_parser = subparsers.add_parser(
        "status", help="Render a CRD-style status report from the local reconcile state"
    )
    status_parser.add_argument(
        "--state-file",
        default=".cloud-connector-routing-adapter-state.json",
        help="Path to the local reconcile state file",
    )
    status_parser.add_argument(
        "--output",
        help="Optional path to also write the rendered status report as JSON",
    )
    status_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")

    write_status_parser = subparsers.add_parser(
        "write-status",
        help="Patch CRD status subresources from the local reconcile state",
    )
    write_status_parser.add_argument(
        "--state-file",
        default=".cloud-connector-routing-adapter-state.json",
        help="Path to the local reconcile state file",
    )
    write_status_parser.add_argument(
        "--server",
        help="Kubernetes API server URL; if omitted, kubeconfig is used",
    )
    write_status_parser.add_argument("--kubeconfig", help="Path to kubeconfig")
    write_status_parser.add_argument("--context", help="Kubeconfig context name")
    write_status_parser.add_argument("--token", help="Bearer token for the Kubernetes API")
    write_status_parser.add_argument(
        "--verify-tls",
        action="store_true",
        help="Verify Kubernetes API TLS certificates",
    )
    write_status_parser.add_argument("--key", help="Only patch the matching document key")
    write_status_parser.add_argument("--kind", help="Only patch matching document kind")
    write_status_parser.add_argument("--name", help="Only patch matching document name")
    write_status_parser.add_argument(
        "--namespace",
        help="Only patch matching document namespace (defaults to 'default' in state)",
    )
    write_status_parser.add_argument(
        "--cluster-scoped",
        action="store_true",
        help="Build cluster-scoped status URLs instead of namespaced ones",
    )
    write_status_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not patch the API server; only print the planned status patch requests",
    )
    write_status_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")

    controller_parser = subparsers.add_parser(
        "controller",
        help="Poll a CRD document file and orchestrate reconcile, status export, and optional status writeback",
    )
    controller_parser.add_argument("--file", help="Path to YAML or JSON document")
    controller_parser.add_argument(
        "--source",
        choices=("file", "kubernetes"),
        default="file",
        help="Document source for the controller loop",
    )
    controller_parser.add_argument(
        "--state-file",
        default=".cloud-connector-routing-adapter-state.json",
        help="Path to the local reconcile state file",
    )
    controller_parser.add_argument(
        "--status-file",
        help="Optional path for a CRD-style status export JSON report",
    )
    controller_parser.add_argument(
        "--interval-seconds",
        type=float,
        default=30.0,
        help="Polling interval between controller iterations",
    )
    controller_parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single controller iteration and exit",
    )
    controller_parser.add_argument(
        "--max-iterations",
        type=int,
        help="Optional hard stop after this many iterations",
    )
    controller_parser.add_argument(
        "--deleted-retention-seconds",
        type=float,
        default=300.0,
        help="Keep locally deleted documents as tombstones for this many seconds before pruning",
    )
    controller_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changed VyOS commands during each iteration",
    )
    controller_parser.add_argument("--vyos-url", help="VyOS API base URL")
    controller_parser.add_argument("--api-key", help="VyOS API key")
    controller_parser.add_argument(
        "--vyos-timeout",
        type=float,
        default=30.0,
        help="VyOS API timeout in seconds",
    )
    controller_parser.add_argument(
        "--verify-vyos-tls",
        action="store_true",
        help="Verify VyOS API TLS certificates",
    )
    controller_parser.add_argument(
        "--write-status",
        action="store_true",
        help="Write CRD status back to Kubernetes each iteration",
    )
    controller_parser.add_argument(
        "--dry-run-status",
        action="store_true",
        help="Do not patch Kubernetes; only plan the status writes",
    )
    controller_parser.add_argument(
        "--server",
        help="Kubernetes API server URL; if omitted, kubeconfig is used",
    )
    controller_parser.add_argument("--kubeconfig", help="Path to kubeconfig")
    controller_parser.add_argument("--context", help="Kubeconfig context name")
    controller_parser.add_argument("--token", help="Bearer token for the Kubernetes API")
    controller_parser.add_argument(
        "--verify-k8s-tls",
        action="store_true",
        help="Verify Kubernetes API TLS certificates",
    )
    controller_parser.add_argument(
        "--cluster-scoped-status",
        action="store_true",
        help="Build cluster-scoped CRD status URLs instead of namespaced ones",
    )
    controller_parser.add_argument(
        "--source-namespace",
        help="Namespace to watch when --source kubernetes is used",
    )
    controller_parser.add_argument(
        "--cluster-scoped-source",
        action="store_true",
        help="Treat the Kubernetes source CRDs as cluster-scoped",
    )
    controller_parser.add_argument(
        "--resource-kind",
        action="append",
        help="Restrict the Kubernetes source to one or more supported kinds",
    )
    controller_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    controller_parser.add_argument(
        "--enable-leader-election",
        action="store_true",
        help="Enable Kubernetes Lease-based leader election",
    )
    controller_parser.add_argument(
        "--leader-id",
        help="Leader identity (defaults to hostname or PID)",
    )
    controller_parser.add_argument(
        "--lease-namespace",
        help="Namespace for the Lease object (defaults to --source-namespace or 'default')",
    )
    controller_parser.add_argument(
        "--lease-duration-seconds",
        type=int,
        default=15,
        help="Lease duration in seconds (default: 15)",
    )

    apply_parser = subparsers.add_parser("apply", help="Apply translated commands to a VyOS target")
    apply_parser.add_argument("--file", required=True, help="Path to YAML or JSON document")
    apply_parser.add_argument("--vyos-url", required=True, help="VyOS API base URL")
    apply_parser.add_argument("--api-key", required=True, help="VyOS API key")
    apply_parser.add_argument(
        "--vyos-timeout",
        type=float,
        default=30.0,
        help="VyOS API timeout in seconds",
    )
    apply_parser.add_argument(
        "--verify-tls",
        action="store_true",
        help="Verify TLS certificates when talking to VyOS",
    )

    args = parser.parse_args()

    if args.command == "plan":
        result = _translate_documents_from_file(args.file)
        _print_result(result, json_output=args.json)
        return 0

    if args.command == "reconcile":
        documents = load_documents(args.file)
        client = None
        if args.apply:
            _require_vyos_credentials(parser, args.vyos_url, args.api_key)
            client = _build_vyos_client(
                base_url=args.vyos_url,
                api_key=args.api_key,
                verify_tls=args.verify_tls,
                timeout=args.vyos_timeout,
            )

        reconcile_result = reconcile_documents(
            documents,
            VyosTranslator(),
            ReconcileState.load(args.state_file),
            args.state_file,
            apply=args.apply,
            client=client,
            status_file=args.status_file,
        )
        _print_reconcile_result(reconcile_result, json_output=args.json)
        return 0

    if args.command == "status":
        report = build_status_report(ReconcileState.load(args.state_file))
        if args.output:
            from pathlib import Path

            output_path = Path(args.output)
            if output_path.parent != Path("."):
                output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(report.to_json() + "\n")
        _print_status_report(report, json_output=args.json)
        return 0

    if args.command == "write-status":
        report = build_status_report(ReconcileState.load(args.state_file))
        result = _build_kube_status_writer(
            kubeconfig=args.kubeconfig,
            context=args.context,
            server=args.server,
            token=args.token,
            verify_tls=args.verify_tls,
            allow_default_server_for_dry_run=args.dry_run,
        ).write_status(
            report,
            dry_run=args.dry_run,
            selector=_status_selector(args),
            cluster_scoped=args.cluster_scoped,
        )
        _print_write_status_result(result, json_output=args.json)
        return 0

    if args.command == "controller":
        if args.source == "file" and not args.file:
            parser.error("--source file requires --file")
        if args.write_status and not args.server and not args.kubeconfig and not args.dry_run_status:
            parser.error(
                "--write-status requires --server or --kubeconfig unless --dry-run-status is set"
            )
        if args.source == "kubernetes" and not args.server and not args.kubeconfig:
            parser.error("--source kubernetes requires --server or --kubeconfig")

        vyos_client = None
        if args.apply:
            _require_vyos_credentials(parser, args.vyos_url, args.api_key)
            vyos_client = _build_vyos_client(
                base_url=args.vyos_url,
                api_key=args.api_key,
                verify_tls=args.verify_vyos_tls,
                timeout=args.vyos_timeout,
            )

        status_writer = None
        if args.write_status:
            status_writer = _build_kube_status_writer(
                kubeconfig=args.kubeconfig,
                context=args.context,
                server=args.server,
                token=args.token,
                verify_tls=args.verify_k8s_tls,
                allow_default_server_for_dry_run=args.dry_run_status,
            )

        document_source = _build_document_source(args)
        lease_mgr = _build_lease_manager(args)

        result = run_controller(
            source=document_source,
            state_file=args.state_file,
            status_file=args.status_file,
            interval_seconds=args.interval_seconds,
            once=args.once,
            max_iterations=args.max_iterations,
            apply=args.apply,
            vyos_client=vyos_client,
            write_status=args.write_status,
            status_writer=status_writer,
            dry_run_status=args.dry_run_status,
            cluster_scoped_status=args.cluster_scoped_status,
            deleted_retention_seconds=args.deleted_retention_seconds,
            lease_manager=lease_mgr,
        )
        _print_controller_result(result, json_output=args.json)
        return 0

    result = _translate_documents_from_file(args.file)
    client = _build_vyos_client(
        base_url=args.vyos_url,
        api_key=args.api_key,
        verify_tls=args.verify_tls,
        timeout=args.vyos_timeout,
    )
    response = client.configure_commands(result.commands)
    print(json.dumps({"translation": _as_dict(result), "vyos_response": response}, indent=2))
    return 0


def _print_result(result: TranslationResult, json_output: bool) -> None:
    if json_output:
        print(json.dumps(_as_dict(result), indent=2))
        return

    if result.commands:
        print("# Commands")
        for command in result.commands:
            print(command)

    if result.warnings:
        print("\n# Warnings")
        for warning in result.warnings:
            print(f"- {warning}")

    if result.unsupported:
        print("\n# Unsupported")
        for unsupported in result.unsupported:
            print(f"- {unsupported}")


def _as_dict(result: TranslationResult) -> dict[str, list[str]]:
    return {
        "commands": result.commands,
        "warnings": result.warnings,
        "unsupported": result.unsupported,
    }


def _translate_documents_from_file(path: str) -> TranslationResult:
    translator = VyosTranslator()
    result = TranslationResult()
    for document in load_documents(path):
        result.extend(translator.translate(document))
    return result


def _build_vyos_client(
    *,
    base_url: str,
    api_key: str,
    verify_tls: bool,
    timeout: float,
) -> VyosApiClient:
    return VyosApiClient(
        base_url=base_url,
        api_key=api_key,
        verify_tls=verify_tls,
        timeout=timeout,
    )


def _build_kube_status_writer(
    *,
    kubeconfig: str | None,
    context: str | None,
    server: str | None,
    token: str | None,
    verify_tls: bool,
    allow_default_server_for_dry_run: bool,
) -> KubeStatusWriter:
    return KubeStatusWriter(
        _build_kube_connection(
            kubeconfig=kubeconfig,
            context=context,
            server=server,
            token=token,
            verify_tls=verify_tls,
            allow_default_server_for_dry_run=allow_default_server_for_dry_run,
        )
    )


def _build_kube_connection(
    *,
    kubeconfig: str | None,
    context: str | None,
    server: str | None,
    token: str | None,
    verify_tls: bool,
    allow_default_server_for_dry_run: bool = False,
):
    resolved_server = server
    if allow_default_server_for_dry_run and not resolved_server and not kubeconfig:
        resolved_server = "https://kubernetes.default.svc"
    return load_kube_connection(
        kubeconfig=kubeconfig,
        context=context,
        server=resolved_server,
        token=token,
        verify_tls=verify_tls,
    )


def _build_lease_manager(args) -> LeaseManager:
    if not getattr(args, "enable_leader_election", False):
        return NoopLeaseManager()
    connection = _build_kube_connection(
        kubeconfig=args.kubeconfig,
        context=args.context,
        server=args.server,
        token=args.token,
        verify_tls=args.verify_k8s_tls,
    )
    return KubeLeaseManager(
        connection=connection,
        lease_namespace=args.lease_namespace or args.source_namespace or "default",
        leader_id=args.leader_id or "",
        lease_duration_seconds=args.lease_duration_seconds,
    )


def _build_document_source(args) -> FileDocumentSource | KubernetesDocumentSource:
    if args.source == "kubernetes":
        return KubernetesDocumentSource(
            client=KubeDocumentClient(
                _build_kube_connection(
                    kubeconfig=args.kubeconfig,
                    context=args.context,
                    server=args.server,
                    token=args.token,
                    verify_tls=args.verify_k8s_tls,
                )
            ),
            namespace=args.source_namespace,
            cluster_scoped=args.cluster_scoped_source,
            resource_kinds=args.resource_kind,
        )
    return FileDocumentSource(args.file)


def _status_selector(args) -> dict[str, str | None]:
    return {
        "key": args.key,
        "kind": args.kind,
        "name": args.name,
        "namespace": args.namespace,
    }


def _require_vyos_credentials(parser: argparse.ArgumentParser, base_url: str | None, api_key: str | None) -> None:
    if not base_url or not api_key:
        parser.error("--apply requires --vyos-url and --api-key")


def _print_reconcile_result(result, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result.to_dict(), indent=2))
        return

    print("# Reconcile")
    print(f"state file: {result.state_file}")
    if result.status_file:
        print(f"status file: {result.status_file}")
    print(f"apply requested: {'yes' if result.apply_requested else 'no'}")
    print(f"apply performed: {'yes' if result.apply_performed else 'no'}")
    print(f"pending command count: {result.command_count}")

    for item in result.documents:
        print(f"\n## {item.key}")
        print(f"action: {item.action}")
        print(f"in sync: {'yes' if item.in_sync else 'no'}")
        print(f"desired revision: {item.desired_revision}")
        print(f"applied revision: {item.applied_revision or '-'}")
        print(f"desired digest: {item.desired_digest}")
        print(f"applied digest: {item.applied_digest or '-'}")
        print(f"commands: {item.command_count}")
        print(f"warnings: {item.warning_count}")
        print(f"unsupported: {item.unsupported_count}")
        if item.warnings:
            print("warning detail:")
            for warning in item.warnings:
                print(f"- {warning}")
        if item.unsupported:
            print("unsupported detail:")
            for unsupported in item.unsupported:
                print(f"- {unsupported}")


def _print_status_report(report, json_output: bool) -> None:
    if json_output:
        print(report.to_json())
        return

    print("# Status")
    print(f"generated at: {report.generated_at}")
    print(f"documents: {report.document_count}")

    for item in report.documents:
        print(f"\n## {item.key}")
        print(f"phase: {item.phase}")
        print(f"desired revision: {item.desired_revision or '-'}")
        print(f"applied revision: {item.applied_revision or '-'}")
        print(f"warnings: {item.warning_count}")
        print(f"unsupported: {item.unsupported_count}")
        print(f"last result: {item.last_result}")
        if item.last_error:
            print(f"last error: {item.last_error}")
        print("conditions:")
        for condition in item.conditions:
            print(
                f"- {condition.type}: {condition.status} "
                f"({condition.reason})"
            )


def _print_write_status_result(result, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result.to_dict(), indent=2))
        return

    print("# Write Status")
    print(f"dry run: {'yes' if result.dry_run else 'no'}")
    print(f"patches: {len(result.patches)}")
    print(f"skipped: {len(result.skipped)}")
    if result.skipped:
        print("skipped detail:")
        for item in result.skipped:
            print(f"- {item['key']}: {item['reason']}")
    for patch in result.patches:
        print(f"\n## {patch.key}")
        print(f"url: {patch.url}")
        print(f"plural: {patch.plural}")
        print("body:")
        print(json.dumps(patch.body, indent=2))


def _print_controller_result(result, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result.to_dict(), indent=2))
        return

    print("# Controller")
    print(f"once: {'yes' if result.once else 'no'}")
    print(f"source: {result.source}")
    print(f"interval seconds: {result.interval_seconds}")
    print(f"iterations: {len(result.iterations)}")
    for iteration in result.iterations:
        print(f"\n## iteration {iteration.iteration}")
        print(f"ok: {'yes' if iteration.ok else 'no'}")
        if iteration.error:
            print(f"error: {iteration.error}")
            continue
        print(f"changed documents: {iteration.changed_documents}")
        print(f"deleted documents: {iteration.deleted_documents}")
        print(f"pruned documents: {iteration.pruned_documents}")
        print(f"pending commands: {iteration.pending_command_count}")
        print(f"status patches: {iteration.status_patch_count}")
        print(f"vyos apply performed: {'yes' if iteration.apply_performed else 'no'}")
        print(
            f"kubernetes status write performed: "
            f"{'yes' if iteration.status_write_performed else 'no'}"
        )


if __name__ == "__main__":
    sys.exit(main())
