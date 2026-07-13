# Changelog

## 0.5.0.dev0 (Unreleased)

### Capability Retrieval Shadow

- Adds revision-bound `capability-index/v1` build/validate commands and dependency-free lexical Top-K retrieval over
  bounded inventory metadata.
- Adds an opt-in `capabilityRetrieval.mode: shadow` lane whose results never affect legacy route ranking,
  `ActivationIR`, or model-visible hook context.
- Adds redacted `retrieval-result/v1` diagnostics and privacy-preserving latency/candidate measurement fields; raw
  prompts, descriptions, matched substrings, and search tokens are excluded.
- Makes missing, invalid, symlinked, or stale indexes fail open and adds doctor warnings without declaring legacy
  routing unhealthy.
- Adds 12 English contrast fixtures and a separate Recall@3/Top-1 evaluator while preserving the 127-case legacy
  corpus; Korean-only catalog recall remains an explicitly documented first-tranche gap.
- Adds a frozen 240-case paired legacy-vs-retrieval evaluator and prompt-redacted result artifact. The first synthetic
  run shows directional Top-1 gains but blocks behavior promotion on Korean, no-skill semantics, and missing host
  ownership.
- Adds bounded optional host-catalog aliases/capabilities, source-category retrieval evidence IDs, and a
  corpus-informed bilingual calibration report. The pilot improves Korean retrieval directionally but is explicitly
  ineligible for active-catalog promotion.
- Separates labelled Top-K conflicts from inventory eligibility and verifies zero inactive/unavailable candidates in the
  corrected control and pilot runs. Lexical `no-match` is explicitly not semantic `abstain`.
- Adds additive `RoutingObservationV1` records with bounded evidence IDs, explicit unobserved ownership, and
  `observe-only`/`fallback-legacy`/`stop-shadow` actions that never alter legacy selection.
- Adds evaluator-only `PromotionGateV1` with frozen evidence, quality, latency, eligibility, and privacy checks. It can
  only return blocked or eligible for human review and never promotes active behavior automatically. Artifact evidence
  remains blocked unless an explicit local root safely resolves five unique regular files whose actual SHA-256 values
  match the manifest; paths and contents stay out of reports. Stable decision revisions are separated from full
  benchmark-run revisions.
- Blocks review eligibility when expected-abstain lexical no-match recall is below `0.95` or regresses from legacy;
  this remains a conservative no-skill check rather than a claim of semantic abstention.
- Adds deterministic, prompt-redacted exact skill-reference signals and `AutomatedShadowEvidenceV1`. Its collection
  gate can qualify only a prospective explicit-reference slice for automated shadow review; promotion remains blocked,
  authority remains `none`, and no-skill, ownership, independence, or end-to-end quality are not inferred.
- Adds a source-checkout private CAS tool for preserving frozen control/pilot replay inputs with byte-level deduplication,
  `0700`/`0600` permissions, immutable refs, stored-input revalidation, and no source paths in descriptors.

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

### Activation Precision

- Adds immutable `ActivationIR` with separate `activate`, `propose`, and `abstain` runtime dispositions above the
  existing route ranking layer.
- Treats weak, ambiguous, fallback, answer-only, and incomplete-facet matches as candidate-only proposals; a proposal
  explicitly activates no skill. Selection-rationale meta matches hard-abstain and emit no model-visible context,
  while explicit implementation actions override soft explanation wording and hard no-action rules remain dominant.
- Adds per-route `auto`/`propose-only` activation mode and marks the self-referential skill-routing route
  `propose-only`, preventing it from activating itself even when action words are present.
- Activates only the primary skill. Supporting and verification skills remain deferred and are no longer named in the
  model-visible hook block as an automatic bundle.
- Adds optional identifier-safe pattern `facet` values and per-route `activation.requiredFacets`, `scope`, and `mode`
  for deterministic target/action-style eligibility gates in both route schemas.
- Extends `policy-proposal/v2` and the v1/v2 compilers with safe activation facets while preserving old proposals that
  omit those optional fields.
- Adds `route --activation-ir-json`, additive activation data in diagnostics and versioned contracts, and cumulative
  activate/propose/abstain measurement counts.
- Suppresses route and skill recommendation lists from every structured contract when the activation decision abstains.
- Expands the golden corpus with contrastive meta-discussion, weak-candidate, strong-activation, and abstention cases.
- Removes bundled LazyCodex/OMO allowlist entries and generic OMO frontend, debugging, refactor, code, and code-docs
  routes. Generic implementation stays model-native, and prompts with explicit code-edit plus documentation actions
  now abstain instead of activating a documentation-only substitute. Docs-only prose about code artifacts remains
  routable.

### Safety

- Policy compilation rejects stale inventory revisions, unavailable or ambiguous skills, unsupported fields, unsafe
  regex forms, and overlapping positive examples.
- Stage and promotion re-check the current inventory and host-catalog revisions. Promotion evidence is bound to the
  exact config revision and only counts shadow decisions that would win after activation.
- App-generated regexes use a restricted, bounded subset; unbounded quantifiers, quantified alternation, lookaround,
  and backreferences are rejected.
- Custom activation regexes use a conservative subset, with only the exact audited bundled defaults allowlisted.
- Sync apply writes only the inventory manifest. Policy compilation writes a separate candidate file, and stage and
  promotion back up the active route config before mutation.
- Skill scanning rejects leaf symlinks, symlinked parents, and metadata outside the selected root before reading it;
  additive `scanIssues` expose only relative locators and reason codes.
- Hook context uses validated pattern IDs and a fixed router-owned reason, so descriptions and proposal v1 reason/label
  text cannot become model-visible routing instructions.
- Invalid Policy IR fails open, configured inventory canonical IDs are enforced before runtime scoring, and versioned
  recommendation adapters exclude shadow and disabled routes.
- Schema v2 compilation extends an existing `allowedSkills` list for new shadow routes and preserves all prior entries.
- Inventory resolution rejects canonical IDs shared across usable configured names and drops unresolved default
  verification references before model-visible hook context is built.
- Implicit install and doctor probes preserve schema v1 or v2, and policy/catalog writes reject symlinked parent
  components before backup or atomic replacement.
- Install automatically upgrades the bundled router skill only when its managed manifest digest still matches. Modified,
  preserved, symlinked, unsafe, or unowned copies remain untouched without explicit `--force`.
- Installer dry-run validates pending recovery journals without rolling back or deleting files, and hook configuration
  is confined before reads.
- Install and uninstall identify owned hooks by exact normalized command or a valid confined ownership manifest instead
  of marker substrings; unsafe manifest parents and foreign lookalike hooks are preserved.
- Release checksum verification rejects empty, incomplete, duplicate, escaping, and symlinked manifests and requires
  exact artifact-root coverage before hashing.
- Skill metadata parsing is limited to 64 KiB/200 lines, document hashing is streamed with a 1 MiB ceiling, and terminal
  controls are escaped in human sync output. The shipped composite docs exclusion uses a start-anchored scan, while
  activation meta detection uses a linear token scan that preserves the legacy order and single-line boundaries.

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
