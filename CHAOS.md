# Chaos Testing

This runbook focuses on failure injection for the routing adapter and its
current VyOS-compatible target profile rather than only smoke validation.

## Local Chaos Harness

The local harness does not require Venus or the VyOS VM. It drives the real
controller, reconcile, state, and status code with scripted failures:

```bash
python3 scripts/chaos-hbr-api-local.py run
```

It currently covers:

- VyOS apply timeout followed by clean recovery on rerun
- Kubernetes status write failure after a successful apply, followed by clean
  status convergence on rerun
- watch-loop chaos with one wait failure, document churn, deletion, and
  immediate tombstone pruning
- Kubernetes status patch retry behavior under transport failure and `503`
  responses

Artifacts are written under:

```text
artifacts/private/hbr-chaos-local
```

Each scenario stores its controller results, state, and status payloads so the
failure and recovery path can be inspected after the run.

The public-safe summary for publication is:

```text
artifacts/public/hbr-chaos-local-summary.json
```

## Live Chaos On The Lab VM

When Venus is reachable again, use the lifecycle runner for live chaos against
the real VM:

First, restore the intended guest profile on `venus`:

```bash
sudo ./scripts/venus-vm-profile
```

```bash
ROUNDS=3 MATRIX_CYCLES=2 DEEPER_ITERATIONS=2 \
  scripts/lifecycle-api-matrix-vyos-vm.sh run
```

That complements the local harness by injecting real guest restarts:

- baseline
- `virsh reboot`
- `virsh reset`
- `virsh shutdown` + `virsh start`

and rerunning the API matrix plus the deeper smoke harness after each phase.
