# Changelog

## 0.5.0.dev0 (Unreleased)

### App-Aware Policy Sync

- Adds `catalog`, `sync`, and `policy` CLI workflows for app-provided skill metadata, revisioned inventory drift, and
  app-LLM-generated route proposals.
- Adds bounded frontmatter description extraction so policy context can explain filesystem skills without persisting
  full `SKILL.md` bodies.
- Keeps LLM use out of the runtime hook. Proposals compile into deterministic `shadow` routes layered after preserved
  active routes.
- Adds route lifecycle enforcement, real-shadow-decision feedback linkage, and an explicit promotion gate requiring at
  least five samples, helpful rate of `0.8`, zero harmful samples, and user approval.
- Adds explicit route retirement for removed or disabled skills; retirement keeps the route record and marks it
  `disabled` after stage review.
- Makes doctor detect filesystem and host-catalog inventory staleness and counts missing skill names uniquely.
- Excludes unresolved duplicate skill names and inactive host-confirmed cache entries from automatic route generation.
- Adds one immutable Policy IR shared by runtime, validator, sync, doctor, install smoke, evaluator, and policy compiler.
  Route config v1 and v2 remain supported in their original schema.
- Adds preferred `policy-proposal/v2` with canonical bindings, identifier-safe route/intent/pattern/skill names, and no
  free-form route reason or pattern label. Proposal v1 remains as a deprecated compatibility input.
- Adds schema-tagged per-reference resolution results and distinct missing, inactive, ambiguous, canonical-missing, and
  canonical-mismatch findings to sync JSON.
- Preserves v1 or v2 base policy shape during compilation and adds new routes only as shadow candidates.

### Safety

- Policy compilation rejects stale inventory revisions, unavailable or ambiguous skills, unsupported fields, unsafe
  regex forms, and overlapping positive examples.
- Stage and promotion re-check the current inventory and host-catalog revisions. Promotion evidence is bound to the
  exact config revision and only counts shadow decisions that would win after activation.
- App-generated regexes use a restricted, bounded subset; unbounded quantifiers, quantified alternation, lookaround,
  and backreferences are rejected.
- Sync apply writes only the inventory manifest. Policy compilation writes a separate candidate file, and stage and
  promotion back up the active route config before mutation.
- Skill scanning rejects leaf symlinks, symlinked parents, and metadata outside the selected root before reading it;
  additive `scanIssues` expose only relative locators and reason codes.
- Hook context uses validated pattern IDs and a fixed router-owned reason, so descriptions and proposal v1 reason/label
  text cannot become model-visible routing instructions.
- Invalid Policy IR fails open, configured inventory canonical IDs are enforced before runtime scoring, and versioned
  recommendation adapters exclude shadow and disabled routes.
- Schema v2 compilation extends an existing `allowedSkills` list for new shadow routes and preserves all prior entries.
- Install automatically upgrades the bundled router skill only when its managed manifest digest still matches. Modified,
  preserved, symlinked, unsafe, or unowned copies remain untouched without explicit `--force`.

## 0.4.0 (2026-07-10)

### Highlights

- Adds opt-in versioned route-result, structured recommendation, and compact Hook IR diagnostics while preserving the
  v0.3 advisory hook output by default.
- Adds opt-in shadow measurement with automatic decision/completion events and explicit cumulative outcome reporting.
- Hardens install, recovery, doctor, and uninstall behavior with ownership manifests and symlink confinement.
- Uses one verified distribution bundle for both PyPI and GitHub Release publication.

### Features

- Schema v2 routing policies with stable route/pattern IDs, weighted evidence, deterministic ranking, and bounded
  multi-route diagnostics.
- Path-redacted skill inventory with canonical identity and conservative unknown eligibility.
- `inject`, `shadow`, and `off` activation modes.
- `outcome` and `report` commands with revision-aware native/inject rescue, harm, and net-win metrics.

### Fixes And Safety

- Authoritative invalid config fails open instead of silently falling through to a lower-precedence policy.
- Install mutations use rollback snapshots and a path-confined recovery journal.
- Removal preserves modified, user-owned, and symlinked artifacts.
- Doctor skips executable smoke checks after managed runtime drift is detected.
- Uninstall refuses to read or write through a symlinked `hooks.json` target.
- Completion correlation requires matching session and turn hashes.
- Duplicate outcomes are deduplicated, conflicting labels are excluded, and pairs do not cross policy/config revisions.
- Unknown measurement schemas with valid timestamps remain bounded and preserved but are ignored by the current report.

### Compatibility

- The default route config and hook output remain compatible with the observed v0.3 contract.
- Measurement is disabled by default; enabling it adds a conditional `Stop` hook.
- Structured recommendation, Hook IR, and schema v2 remain opt-in.
- Python 3.9 or newer is required. Native Windows remains unsupported.

### Upgrade

1. Upgrade the package with `pipx upgrade lazy-skill-router`.
2. Run `lazy-skill-router install --dry-run` and review the planned hook changes.
3. Run `lazy-skill-router install` to refresh the standalone copied hook runtime.
4. Run `lazy-skill-router doctor`.

Existing `routes.json` is preserved unless `--overwrite-routes` is supplied. Review custom route and logging settings
before enabling measurement.

### Rollback

1. While v0.4 is still installed, run
   `lazy-skill-router install --disable-measurement --activation-mode inject` to remove the `Stop` hook and disable the
   v0.4 measurement journal writer.
2. Run `lazy-skill-router uninstall` to remove the current hook registrations.
3. Install `lazy-skill-router==0.3.0` with pipx.
4. Restore a v0.3-compatible route config when a custom schema v2 policy was enabled.
5. Run `lazy-skill-router install` and `lazy-skill-router doctor`.

Local measurement journals are user data and are intentionally preserved by uninstall.

### Known Limits

- Completion is a lifecycle signal, not proof of task success.
- Experiment manifests, corpus versions, objective evaluator automation, random assignment, and confidence intervals
  are not implemented.
- Runtime auth, MCP, dependency, and managed-policy eligibility remain unknown without a trusted runtime source.
- Full hosted CI passes on Ubuntu 22.04 x64 / Python 3.9 and Ubuntu 24.04 x64 / Python 3.11 and 3.14. Other Linux
  distributions and architectures remain experimental. WSL is unverified and outside the 0.4.0 support claim.
- The released v0.3.0 logger uses `datetime.UTC`; on Python 3.9, disable v0.4 measurement before downgrading as shown
  above so the restored v0.3 hook does not enter that incompatible opt-in logging path.

### Verification

- 133 unit tests pass on the local default Python and Python 3.9.
- Route evaluation passes 106 prompts across 15 categories.
- Fresh wheel/sdist, isolated pipx, install, hook, report, doctor, and uninstall flows pass locally.
- An isolated macOS Codex CLI 0.144.0 canary recorded correlated shadow `UserPromptSubmit` and `Stop` events without
  persisting the raw prompt or response.
- The documented `v0.3.0 -> v0.4.0 -> v0.3.0` rollback sequence passes with Python 3.9 after v0.4 measurement is
  disabled before downgrade.
- PR #2 hosted Ubuntu/Python 3.9 CI passes the full source and package workflow.
- PR #3 hosted Linux compatibility matrix run 29094967366 passes all three platform combinations and the aggregate
  `verify` gate.
