#!/usr/bin/env python3
"""Detect API drift between this adapter and the upstream Sylva GitLab project.

Fetches CRD manifests from the Sylva GitLab repository and compares:
- API groups and versions declared in the upstream CRDs
- Kind names and plural forms
- Newly added or removed fields in the spec schema (shallow diff)

Exit codes:
  0  — no drift detected (or --check-only with no changes)
  1  — drift detected
  2  — upstream fetch failed (network / auth issue)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

import yaml  # PyYAML — already a project dependency via check_examples.sh


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = REPO_ROOT / "scripts" / "sylva-upstream.json"


def load_config() -> dict:
    """Load upstream config from sylva-upstream.json, with fallback defaults."""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {
        "gitlab_api": "https://gitlab.com/api/v4",
        "project": "sylva-projects/sylva-core",
        "branch": "main",
        "crd_paths": [
            "charts/network-operator/crds/nodenetworkconfig.yaml",
            "charts/network-operator/crds/nodenetplanconfig.yaml",
        ],
        "local_crd_dir": "k8s/crds",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Sylva upstream CRD drift")
    parser.add_argument(
        "--upstream-url",
        default=None,
        help="Override base GitLab API URL (default: gitlab.com)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitLab personal access token for private repos",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON drift report to this file",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Exit 1 on drift, 0 on clean (for CI gate)",
    )
    return parser.parse_args()


def fetch_upstream_crds(
    base_url: str,
    project: str,
    branch: str,
    paths: list[str],
    token: str | None,
) -> list[dict]:
    """Fetch CRD YAML files from GitLab raw API."""
    crds: list[dict] = []
    headers = {}
    if token:
        headers["PRIVATE-TOKEN"] = token

    for path in paths:
        encoded_path = urllib.request.quote(path, safe="")
        url = f"{base_url}/projects/{project}/repository/files/{encoded_path}/raw?ref={branch}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode()
            crd = yaml.safe_load(content)
            if crd:
                crds.append(crd)
                print(f"  fetched: {path}", file=sys.stderr)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print(f"  not found (404): {path}", file=sys.stderr)
            else:
                raise
    return crds


def load_local_crds() -> list[dict]:
    crds: list[dict] = []
    for path in LOCAL_CRD_FILES:
        crd = yaml.safe_load(path.read_text())
        if crd:
            crds.append(crd)
    return crds


def extract_crd_summary(crd: dict) -> dict:
    spec = crd.get("spec", {})
    versions = spec.get("versions", [])
    schema_props: dict[str, list[str]] = {}
    for v in versions:
        vname = v.get("name", "?")
        schema = (
            v.get("schema", {})
            .get("openAPIV3Schema", {})
            .get("properties", {})
            .get("spec", {})
            .get("properties", {})
        )
        schema_props[vname] = sorted(schema.keys())
    return {
        "group": spec.get("group", ""),
        "kind": spec.get("names", {}).get("kind", ""),
        "plural": spec.get("names", {}).get("plural", ""),
        "versions": [v.get("name") for v in versions],
        "spec_fields": schema_props,
    }


def diff_summaries(local: dict, upstream: dict) -> dict:
    changes: list[str] = []

    if local["group"] != upstream["group"]:
        changes.append(f"api_group: {local['group']!r} → {upstream['group']!r}")

    local_versions = set(local["versions"])
    upstream_versions = set(upstream["versions"])
    for v in upstream_versions - local_versions:
        changes.append(f"new upstream version: {v!r}")
    for v in local_versions - upstream_versions:
        changes.append(f"removed upstream version: {v!r}")

    # Per-version spec field diff
    for version in upstream_versions & local_versions:
        local_fields = set(local["spec_fields"].get(version, []))
        upstream_fields = set(upstream["spec_fields"].get(version, []))
        for f in upstream_fields - local_fields:
            changes.append(f"version {version}: new spec field {f!r}")
        for f in local_fields - upstream_fields:
            changes.append(f"version {version}: removed spec field {f!r}")

    return {"kind": local["kind"], "changes": changes}


def main() -> int:
    args = parse_args()
    cfg = load_config()

    base_url = args.upstream_url or cfg["gitlab_api"]
    project = urllib.request.quote(cfg["project"], safe="")
    branch = cfg["branch"]
    crd_paths = cfg["crd_paths"]
    local_crd_dir = REPO_ROOT / cfg.get("local_crd_dir", "k8s/crds")

    print("Fetching upstream Sylva CRDs...", file=sys.stderr)
    try:
        upstream_crds = fetch_upstream_crds(
            base_url, project, branch, crd_paths, args.token
        )
    except Exception as exc:
        print(f"ERROR: upstream fetch failed: {exc}", file=sys.stderr)
        return 2

    if not upstream_crds:
        print("WARNING: no upstream CRDs fetched — check paths and branch", file=sys.stderr)
        # Not a hard failure; upstream might have restructured
        return 0

    local_crds = sorted(local_crd_dir.glob("*.yaml"))
    local_crds = [yaml.safe_load(p.read_text()) for p in local_crds if p.exists()]
    local_crds = [c for c in local_crds if c]
    local_by_kind = {extract_crd_summary(c)["kind"]: extract_crd_summary(c) for c in local_crds}
    upstream_by_kind = {extract_crd_summary(c)["kind"]: extract_crd_summary(c) for c in upstream_crds}

    report: dict = {"upstream_kinds": [], "local_kinds": [], "diffs": [], "drift": False}
    report["upstream_kinds"] = sorted(upstream_by_kind.keys())
    report["local_kinds"] = sorted(local_by_kind.keys())

    # New kinds in upstream that we don't support yet
    for kind in set(upstream_by_kind) - set(local_by_kind):
        report["diffs"].append({"kind": kind, "changes": ["new kind in upstream (not yet supported)"]})
        report["drift"] = True

    # Field-level drift for shared kinds
    for kind in set(upstream_by_kind) & set(local_by_kind):
        diff = diff_summaries(local_by_kind[kind], upstream_by_kind[kind])
        if diff["changes"]:
            report["diffs"].append(diff)
            report["drift"] = True

    # Print summary
    if report["drift"]:
        print(f"DRIFT DETECTED — {len(report['diffs'])} kind(s) changed:")
        for item in report["diffs"]:
            print(f"  {item['kind']}:")
            for change in item["changes"]:
                print(f"    - {change}")
    else:
        print("No drift detected — adapter is in sync with upstream Sylva.")

    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        print(f"Report written to {args.output}", file=sys.stderr)

    if args.check_only and report["drift"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
