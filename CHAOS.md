# Chaos Testing

This runbook focuses on failure injection for the routing adapter and its
current VyOS-compatible target profile rather than only smoke validation.

## Local Chaos Harness

The local harness does not require Venus or the VyOS VM. It drives the real
controller, reconcile, state, and status code with scripted failures:

```bash
python3 scripts/chaos-hbr-api-local.py run
```

## Scenarios (12)

1. **vyos-timeout-recovery** — VyOS API timeout, then clean recovery on retry
2. **status-writer-failure-recovery** — Kubernetes 409 conflict on status write,
   apply state preserved, convergence on retry
3. **watch-churn-and-prune** — watch failure, document update, two deletions with
   immediate tombstone pruning (retention=0)
4. **k8s-patch-retry** — transport loss then 503 on HTTP PATCH, retry with
   backoff, final success
5. **commit-failure-rollback** — VyOS commit returns success=false, discard_pending
   called, state not advanced, retry succeeds
6. **cluster-scoped-patch-url** — KubeStatusWriter._patch_plan URL omits
   /namespaces/ when cluster_scoped=True
7. **cluster-scoped-status-wiring** — cluster_scoped_status=True forwarded to
   status writer on apply and noop iterations
8. **route-map-apply-failure-and-retry** — BGP filter commands in failed batch,
   discard, retry with route-map present, noop after success
9. **leader-election-skip-apply** — non-leader makes zero VyOS calls, leader
   applies normally
10. **informer-event-queue** — 2 iterations via scripted source push, both
    applied, route change detected in second iteration
11. **lease-acquire-exception** — controller survives when lease_manager.acquire()
    throws RuntimeError
12. **lease-renewal-multi-cycle** — 3 iterations: leader, leader, non-leader;
    acquire() called 3 times

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
