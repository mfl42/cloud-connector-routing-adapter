# Artifact Layout

This repository keeps test artifacts in two buckets:

- `artifacts/public/`
  - publication-safe summaries intended to stay in the repo
  - no workstation-specific absolute paths
  - no lab-specific credentials or private host defaults
- `artifacts/private/`
  - raw local and lab outputs
  - may include absolute local paths, transient state files, and environment-specific details
  - ignored by Git by default

Current public summaries:

- `artifacts/public/hbr-boundary-local-summary.json`
- `artifacts/public/hbr-chaos-local-summary.json`
- `artifacts/public/hbr-fuzz-local-summary.json`

If you run the local or live harnesses again, keep their default output under
`artifacts/private/` unless you have explicitly sanitized the results for
publication.
