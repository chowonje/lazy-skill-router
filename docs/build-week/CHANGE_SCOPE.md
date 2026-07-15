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
- A local-only CI Relay judge fixture, explicit demo policy, and Desktop scene materializer. These artifacts do not
  alter the runtime, default policy, host installation, wheel, or source-distribution contents.
- A fact-bounded README collaboration narrative and
  [`DEVPOST_SUBMISSION.md`](DEVPOST_SUBMISSION.md) draft that distinguish Architecture 3 from the submission-period
  stabilization work.

## Deliberately excluded

- Top-K activation, automatic promotion, threshold reductions, host installation, tag creation, or release publication.
- Local measurement journals and the stale diagnostic-event known-issue note.
- New independently authored Korean candidate metadata, anchored-v2 bilingual overlays, and local candidate catalogs.
  The already-public baseline pilot fixture remains available only as historical frozen replay material.
- The portable beta evaluator/report bundle and release workflow changes; these remain a separate release-gate tranche.
- Final screenshots and recorded video. The reproducible fixture, prompts, and text draft are prepared, but media
  capture remains a separate human-review step.

## Evidence boundary

| Evidence | Timestamp | Meaning |
| --- | --- | --- |
| `6123ffe3acdc7ae7b35082ab8938d77fc8070872` | 2026-07-13 11:34 KST | Public Architecture 3 baseline created before the submission period. |
| `561732d17c6ad479ba07b2b9cab73dcb05333f90` | 2026-07-15 17:57 KST | Submission-period runtime and managed-write stabilization. |
| `a62473470b120703f929a4026948550db1384627` | 2026-07-15 17:57 KST | Submission-period scope and claim-boundary documentation. |
| `0fc72a1c2345d10c3d554e48f1f5194bdfd44f13` | 2026-07-16 08:09 KST | Submission-period judge path, sample fixture, CI coverage, collaboration narrative, and Devpost draft. |
| Codex session `019f522b-8db5-7211-84d4-889d8c9d9de8` | before submission period | Prior Architecture 3 design and implementation history. |
| Codex session `019f6362-9c9b-76e2-b3d1-cffb12ebfc9d` | during submission period | GPT-5.6 implementation, verification, and judge-preparation history for the stabilization tranche. |

The Devpost submission should use the submission-period Codex session as the primary evidence for the judged
extension and retain the earlier session only as historical provenance.

## Claim boundary

The capability lane is a preview and observation surface. A candidate does not become an activated skill, and a passed
evaluation can at most qualify evidence for human review. This branch makes no production-quality or release-readiness
claim until the documented checks are rerun from the clean worktree.

## Verification

Local verification on 2026-07-16 KST passed:

- 445 project tests;
- 127 routing prompts across 16 categories;
- 4 judge-demo materializer tests and 6 CI Relay fixture tests;
- all three documented judge routing decisions and both route-policy validations;
- Ruff lint and format checks across 73 files;
- wheel and source-distribution build plus Twine metadata checks;
- 445 tests from the extracted source distribution;
- isolated wheel install, route, install, doctor, and uninstall smoke checks.

The public privacy preflight found no secret or PII block. It returned `WARN` because the optional model-based privacy
filter was unavailable and because date/version strings triggered phone-number heuristics; those findings were manually
reviewed as false positives. The intentionally vulnerable demo fixture remains a consciously accepted public warning
and is excluded from package artifacts.

The first public GitHub Actions run on [PR #9](https://github.com/chowonje/lazy-skill-router/pull/9) passed on Ubuntu with
Python 3.9, 3.11, and 3.14 at commit `354637bb629ec1a86c9dfb26677bf175fd7ce79a`. Every later commit must pass the same
matrix before merge. A clean run does not broaden the included scope or authorize a tag, host installation, activation,
or release.
