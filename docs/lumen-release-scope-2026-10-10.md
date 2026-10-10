# Lumen release scope and protected integration

Checkpoint: 2026-10-10 14:50 UTC. This is a scope review, not a release-complete claim.

## Candidate

The candidate is based on 86c55233c76ec345a91c3a226686c3c84a4c72b0 in isolated workspace 520b06121cc249f7b5c67da858426a77. The remote main ref still matched that SHA at review. Remote v1.2.179 was absent; the existing local version synchronization is 1.2.179. Recheck both immediately before publication.

Include the accepted Canvas/media core, typed RunPlan, immutable intent and recovery guards, exact historical branching, freshness/preparation recovery, and their tests. Include the existing Phase A Canvas preview/readiness and notification placement prerequisites: Toast owns a portal inside the active modal, the shared modal layer synchronizes isolation and keyboard focus, and composer/navigation obstacle markers keep interactive notifications clear of controls. These shared UI changes require the final notification and modal browser coverage; they are not included merely because they happened to be dirty.

The complete immutable path/hash release manifest is to be generated after the final browser and performance gates. Runtime build directories, fixture-generated media/databases, raw local logs and tmp/var artifacts are evidence, not production source staging candidates.

## Governance prerequisite reviewed separately

The original dirty known-defects.json changes thirteen fixed_commit references from 1eb29da2e9b36ccb275171df77b1594b0a92d006 to 0224b39fc05d18bea957821a5560deb27d8d821b after the recorded privacy history rewrite. The old commit is not a current HEAD ancestor; the mapped commit is. tests/test_governance_score.py::test_current_known_defects_reference_real_tests_and_commits validates the actual registry and rejects unreachable closed-defect references.

Independent read-only comparison checked all 28 unique application and regression-test files referenced by those entries. Twenty-seven blobs match exactly. The only changed blob is .github/workflows/docker-release.yml; its diff adds the existing forced-push suppression conditions and changes no referenced application fix or regression test. This supports the recorded mapping rather than assigning an arbitrary ancestor to make a gate pass.

Include docs/refactors/known-defects.json and docs/refactors/known-defects-rewrite-provenance.json as this explicit release prerequisite. Preserve the old/new provenance. This is not a claim that the historical defects were newly fixed by the Canvas work. Run the full governance tests and the referenced regressions before final acceptance.

## Verified prerequisite and remote-state checkpoint

At 16:19–16:21 UTC, read-only GitHub checks showed that the latest published
stable release was v1.2.177 (2026-10-06), not v1.2.178. The v1.2.178 tag exists,
but Docker Release run 37966422541 failed in its Python quality gate, including
the unreachable historical fixed_commit references; build and publication jobs
were skipped. CI run 37966417282 failed in the same area. No old workflow was
rerun or cancelled. This strengthens the concrete release need for the reviewed
mapping; it does not authorize unrelated governance edits.

The final candidate passed all 39 governance/manifest tests and manifest lint
(operation 893dc4bdb3204288a84594f7bfc9d469), all 34 referenced Python regression
cases across worker/API/image-job/root suites (52ff103dc7a04cde9557c33932139a50),
and 35 referenced frontend/history geometry cases (3e4ba77f4409476fb5fae5795c73ea75).
The complete Web gate subsequently passed 1,173 tests, type-check, lint and
production build (1ca7cc84a09048c189c7e18469e9ff19). Existing privacy-page lint
advisory remains; no lint error was waived. Full remote CI for the eventual
release commit remains required.

## Explicitly excluded original files

These six pre-existing local audit artifacts are outside this release and must remain byte-identical in the original checkout:

- docs/evidence/Lumen_full_feature_audit_2026-10-05.md
- docs/evidence/audit43-20261002/feed.log
- docs/evidence/pi-sdk-1.0.4-20261006/full-audit.json
- docs/evidence/pi-sdk-1.0.4-20261006/production-audit.json
- docs/evidence/pi-sdk-1.0.4-20261006/repro-runtime-tests.log
- docs/evidence/pi-sdk-1.0.4-20261006/runtime-tests.log

## Integration and release conditions

Verify original HEAD, status, tracked binary diff and all 51 saved dirty hashes before integration. Use per-path baseline/candidate/current comparison, preserve every unrelated file, and stop on an unknown concurrent change. Stage only the reviewed manifest, never git add -A. Check the staged tree independently of unstaged files.

After required tests: run version sync/check, create the reviewed commit, push main without force, push its matching vX.Y.Z tag, and monitor the tag-triggered Docker Release through successful immutable images, GitHub Release and stable aliases. A main-only build is insufficient. Do not initiate a production deployment as part of this source-release task.
