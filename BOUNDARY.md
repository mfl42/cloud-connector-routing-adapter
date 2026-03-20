# Boundary Testing

This runbook covers boundary-value and edge-case testing for the HBR adapter.

Run the local boundary harness with:

```bash
python3 scripts/boundary-hbr-api-local.py
```

It currently checks:

- VRF interface-family boundaries:
  - supported Ethernet attachment
  - inferred but unsupported loopback attachment
  - unknown interface-family rejection
- Static-route and policy-route boundaries:
  - low table value
  - host IPv4 and IPv6 prefixes
  - missing static-route fields
  - policy rules without interface or target table/VRF
  - next-hop warning behavior
- BGP boundaries:
  - address-family inference
  - duplicate family normalization
  - malformed peers without address or remote-as
  - unsupported peer fields
- Netplan boundaries:
  - IPv6 default-route translation
  - hostish IPv6 prefixes
  - missing route `via`
- Reconcile boundary:
  - zero-command documents converging without an API call

Artifacts are written under:

```text
artifacts/private/hbr-boundary-local
```

The public-safe summary for publication is:

```text
artifacts/public/hbr-boundary-local-summary.json
```
