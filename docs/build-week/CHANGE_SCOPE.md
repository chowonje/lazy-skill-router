# Build Week change scope

## Baseline

- Public-source baseline: `6123ffe3acdc7ae7b35082ab8938d77fc8070872`
- Working branch: `agent/build-week-judge-demo`
- This branch contains curated post-baseline stabilization work. The baseline project and its earlier features are not
  presented as work created during this change window.

## Included

- A shared 4,096-character input boundary that abstains before regex, inventory, or retrieval work.
- Conservative PolicyIR validation for lifecycle fields, bounded regexes, finite numeric values, and schema-compatible
  ranking.
- Managed-root confinement across install, uninstall, policy, and sync writes.
- Transactional rollback for install and default-sync bundles. Policy updates use confined atomic single-file
  replacement; uninstall performs confined sequential removal of verified owned files.
- One revision-aligned default sync bundle: inventory, capability-index v2, then install manifest as the commit marker.
- Measurement-pure dry-run and structured diagnostics; only the actual hook writes redacted decision events.
- Dual-read capability indexes, with v2 product builds and an anchored-v2, non-activating CLI/shadow preview.
- Characterization and regression tests for the above boundaries.
- An explicit source-distribution allowlist for public evaluation tools, frozen fixtures, documentation, and tests;
  historical result reports and local candidate artifacts remain excluded.

## Deliberately excluded

- Top-K activation, automatic promotion, threshold reductions, host installation, tag creation, or release publication.
- Local measurement journals and the stale diagnostic-event known-issue note.
- New independently authored Korean candidate metadata, anchored-v2 bilingual overlays, and local candidate catalogs.
  The already-public baseline pilot fixture remains available only as historical frozen replay material.
- The portable beta evaluator/report bundle and release workflow changes; these remain a separate release-gate tranche.
- Demo screenshots, video, and final submission copy; those belong to the next judge-demo tranche after this branch is
  verified.

## Claim boundary

The capability lane is a preview and observation surface. A candidate does not become an activated skill, and a passed
evaluation can at most qualify evidence for human review. This branch makes no production-quality or release-readiness
claim until the documented checks are rerun from the clean worktree.

## Verification

Verification results are recorded in the final handoff for this worktree. A clean test run does not broaden the
included scope or authorize push, tag, host installation, activation, or release.
