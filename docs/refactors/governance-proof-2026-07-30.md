# Lumen Governance Proof

- Date: `2026-07-30`
- Version: `1.2.83`
- Tag: `v1.2.83`
- Commit: `90552af95b07fd7064e38cf800c8a0f136aee7d5`
- Governance score: **10.000/10**
- Hard gates: **passed**
- Docker Release run: `30539286947`
- Release status: **latest stable**

## Machine Evidence

All commit-bound governance evidence checks passed:

- Full repository test plan.
- Fault matrix and sidecar recovery/delivery faults.
- Migration, release, updater, and rollback faults.
- Storage consistency and recovery proof.
- Observability metrics.
- Web domain boundaries and user-state isolation.
- Dead-code, facade, runtime-state, architecture, complexity, and baseline gates.
- Signed-image and supply-chain checks.
- End-to-end release proof.

The release quality gate independently passed in GitHub Actions, including
Python tests, Web type-check and production build, Compose validation, Docker
build smoke, and image start smoke.

## Stable Image Proof

The `v1.2.83`, `v1.2`, `v1`, and `latest` aliases resolve to the release
manifest source digest for every service:

| Service | Source digest |
| --- | --- |
| API | `sha256:6d7a0f3e7321581b283a7df6f8ec07b64c3cfc4fd40123f1b949f732f7bbe008` |
| Worker | `sha256:af23e0408b5a2da1bd38f94cc6438a6817d4468231917a0675476fd42017c9b4` |
| TgBot | `sha256:38768df6d7dbb0422500f82205fa85093e6d325712118e534b174fbdca18d267` |
| Web | `sha256:6f7d6d57b31c854aabc57d5d08ceec4c5bddb7207365b44377f982c0e43c869e` |

## Residual Risk

- Open P0 defects: `0`.
- Open P1 defects: `0`.
- Open registered defects of any severity: `0`.
- Architecture budget retains `2` grandfathered runtime-coupling findings.
- Runtime-state ledger retains `13` grandfathered mutable runtime instances.
- Facade retirement inventory retains `6` registered compatibility APIs.
- Complexity audit retains `5` multi-dimensional findings and `0`
  role-ceiling violations.

These remaining registered findings are bounded by monotonic baselines and do
not fail the 9/10 governance target.
