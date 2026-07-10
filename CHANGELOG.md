# Changelog

## 0.4.0 (Unreleased)

### Highlights

- Adds opt-in versioned route-result, structured recommendation, and compact Hook IR diagnostics while preserving the
  v0.3 advisory hook output by default.
- Adds opt-in shadow measurement with automatic decision/completion events and explicit cumulative outcome reporting.
- Hardens install, recovery, doctor, and uninstall behavior with ownership manifests and symlink confinement.

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

1. Run `lazy-skill-router uninstall` to remove the current hook registrations.
2. Install `lazy-skill-router==0.3.0` with pipx.
3. Restore a v0.3-compatible route config when a custom schema v2 policy was enabled.
4. Run `lazy-skill-router install` and `lazy-skill-router doctor`.

Local measurement journals are user data and are intentionally preserved by uninstall.

### Known Limits

- Completion is a lifecycle signal, not proof of task success.
- Experiment manifests, corpus versions, objective evaluator automation, random assignment, and confidence intervals
  are not implemented.
- Runtime auth, MCP, dependency, and managed-policy eligibility remain unknown without a trusted runtime source.
- Linux support remains experimental until this branch passes hosted CI. WSL is unverified.

### Verification

- 131 unit tests pass on the local default Python and Python 3.9.
- Route evaluation passes 106 prompts across 15 categories.
- Fresh wheel/sdist, isolated pipx, install, hook, report, doctor, and uninstall flows pass locally.
