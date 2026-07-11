# Architecture

## Purpose

`lazy-skill-router` is a small Codex hook project that recommends local skills for the current prompt. It helps Codex notice the right skill early, but the recommendation is advisory. The agent must still inspect the task, repository state, and higher-priority instructions before acting.

The current shipped public behavior is tracked in [`CURRENT_PUBLIC_CONTRACT.md`](CURRENT_PUBLIC_CONTRACT.md).

## Runtime Flow

1. Codex sends a `UserPromptSubmit` hook event to `lazy_skill_router.py`.
2. The hook adapter reads the prompt from CLI options or hook JSON on stdin.
3. `lazy_skill_router_core.py` resolves route config from an explicit path, `$LAZY_SKILL_ROUTER_CONFIG`, installed Codex config, or `routes.default.json`. Explicit and environment paths are authoritative, and an existing installed config is authoritative over the bundled default. A selected authoritative config that cannot be read or parsed produces an empty route table instead of falling through to a lower-trust source.
4. `lazy_skill_router_policy_ir.py` normalizes route config v1 or v2 into immutable `PolicyIR` records. The routing
   engine checks normalized regex patterns, applies the skill allowlist, scores all matching candidates, and returns the
   highest-ranked route.
5. Route lifecycle is evaluated before delivery. `active` routes may inject, `shadow` routes are measured but never
   injected, and `disabled` routes are ignored. Global `activation.mode` can still disable injection or all routing.
6. The adapter injects a `<lazy-skill-router>` context block in `inject` mode, emits nothing model-visible in `shadow`
   mode, or skips routing in `off` mode.
7. When measurement is enabled, the decision is appended to a bounded local event journal and a conditional `Stop` hook
   records turn completion without reading or storing the assistant response.

The default path remains the v1 top-1 compatibility surface. Opt-in shadow paths normalize the same prompt into
route-result v2, structured recommendation v1, or compact Hook IR. Schema v2 policies use stable IDs, intent,
capability requirements, explicit skill bindings, weighted pattern evidence, and post-selection fallback.

The hook is intentionally fail-open. Missing authoritative config, invalid JSON, invalid route entries, unreadable files, broken installed-config symlinks, and malformed hook input degrade to no recommendation. The bundled default remains available only when no override is selected and no installed config exists.

## Main Modules

### `lazy_skill_router_core.py`

Pure routing engine and activation-mode policy. It consumes normalized routes, owns pattern matching, confidence labels,
answer-only detection, recommendation formatting, and opt-in visible route notices.

The core should remain independent from Codex hook I/O so dry-run, tests, and evals can exercise routing behavior without installing the hook.

### `lazy_skill_router_policy_ir.py`

Shared immutable policy boundary for runtime, validator, sync, doctor, install, evaluator, and policy compiler. It parses
v1 direct references and v2 capability requirements/bindings into one `PolicyIR`, resolves configured names to unique
canonical inventory identities, reports missing/inactive/ambiguous/mismatched references with per-reference resolution
records, and selects the first eligible active primary for smoke tests. Input files retain their original schema and are
never rewritten merely by parsing.

The runtime rejects a Policy IR containing any parser error. When a configured inventory snapshot is available, active
and shadow routes are resolved before scoring and any route with an unresolved reference is excluded. A configured but
invalid inventory also fails open; the legacy no-inventory source path remains available for v1 compatibility.

### `lazy_skill_router_scoring.py`

Candidate matching and ranking. It owns route dataclasses, confidence calculation, optional `priority` and `weight` scoring, allowlist filtering, and `fallback` handling.

Route patterns may be raw regex strings or labeled pattern objects. Matching uses the regex. Labels and regexes remain
available in local diagnostics; model-visible recommendation context uses only validated pattern IDs.

Fallback routes are only selected when no non-fallback route matches. Use them for broad categories such as generic implementation work.

The v2 ranker uses score, match strength, and route ID for deterministic ordering independent of JSON array order.
Pattern weights default to `1`, preserving v1 scoring, while explicit v2 weights contribute to match strength.

### `lazy_skill_router_contracts.py`

Versioned, recommendation-only output models. It builds route-result v2, structured recommendation v1, and compact
Hook IR from the same ranked evidence. It excludes raw prompts and absolute paths, marks match strength as
`not_probability`, preserves agent override, and never represents availability or config trust as authorization.
All three adapters use only resolved `active` routes; `shadow` remains measurement-only and `disabled` remains ignored.

### `lazy_skill_router_inventory.py`

Generated skill inventory and runtime loader. The manifest uses canonical provider identity, path-redacted locator
references, content digests, bounded `name`/`description` frontmatter metadata, revisions, and ambiguity-preserving
duplicate handling. It never stores the remaining `SKILL.md` body. Runtime/auth/MCP/dependency states remain `unknown`
until a trusted source can verify them.

The inventory also compares persisted and current revisions, reporting added, removed, content-changed, and ambiguous
skills without storing absolute paths or full skill bodies. The scanner refuses leaf symlinks, symlinked parents, and
metadata resolving outside its skill root before reading them. Rejections expose only a root-relative locator and reason
code through `scanIssues`.

### `lazy_skill_router_host_catalog.py`

Validates and revisions skill metadata supplied by the current host app. A complete catalog can mark filesystem-only
cache entries inactive; an incomplete catalog cannot. System skills that have no local `SKILL.md` can still become
available through host provenance. Duplicate filesystem identities remain ambiguous unless the host exposes one
resolved runtime name.

The host LLM writes only a draft. `catalog build` performs schema validation, canonical sorting, and revision hashing.
The runtime hook never imports this module and never calls an LLM.

### `lazy_skill_router_policy.py`

Builds a path-redacted context for the app LLM and advertises `policy-proposal/v2`. V2 accepts identifier-safe route,
intent, pattern, and configured skill names, canonical skill bindings, bounded patterns, and synthetic examples, but no
free-form route reason or pattern label. Proposal v1 remains a deprecated compatibility input; its identifiers pass the
same safety rules and its free-form reason and labels do not reach model-visible context. Compilation preserves the base
schema: v1 bases receive v1 shadow routes, while v2 bases receive deterministic capability IDs and binding objects.
Validation binds proposals to exact inventory and host-catalog revisions, checks skill availability, bounds regexes,
rejects nested quantifiers and example collisions, and never overwrites the active route file during compilation.

Staging verifies the base-config revision and preserves every existing route record. A proposal may explicitly mark a
stale route `disabled` with a retirement reason; it cannot silently delete or otherwise rewrite the route. New routes
are added as shadow candidates. Feedback can attach only to a real logged shadow decision and stores no prompt text.
Each shadow decision records whether that route would beat the current active winner after promotion. Promotion
re-checks inventory, host-catalog, and config revisions and requires five same-config winning samples, helpful rate at
least `0.8`, zero harmful samples, no conflicts, and explicit approval. Staging and promotion back up the active route
file before mutation.

### `lazy_skill_router.py`

Codex hook adapter and dry-run CLI. It owns stdin parsing, CLI flags, hook JSON output, and conversion from a prompt string to either diagnostics or Codex `hookSpecificOutput`.

Dry-run diagnostics include the selected route, up to three ranked candidates, human-readable `matchedSignals`,
regex-level `matchedPatterns`, and safe `matchedPatternIds`. The hook output remains smaller and injects only validated
pattern IDs plus a fixed router-owned reason.

### `lazy_skill_router_logging.py` And `measurement.py`

The logging module writes locked, atomic, count- and age-bounded measurement events. Decision and completion records use
hashed prompt/session/turn identifiers and exclude raw prompt, assistant response, transcript path, and working directory.
Unknown event schemas with a valid timestamp are preserved in the bounded journal but excluded from current reports. `measurement.py` appends explicit
objective/human/grader outcome labels and builds cumulative reports. Completion correlation uses the session and turn
hashes together. Duplicate outcomes are deduplicated, conflicting outcomes are excluded, and native/inject pairs never
cross policy/config revision contexts. Completion is a lifecycle observation, not a success label.

### `routes.default.json`

Bundled route policy data. It defines the skill allowlist, confidence threshold, answer-only patterns, and route metadata.

Routes may define optional `priority`, `weight`, and `fallback` fields. Candidate ranking prefers non-fallback routes, then higher score, then higher confidence, then earlier config order.

### `routes.template.json`

Candidate-based route policy data for user-specific config generation. It keeps the same routing metadata as `routes.default.json`, but skill references are expressed as `primaryCandidates`, `supportingCandidates`, and `verificationCandidates`.

### `generate_routes.py`

User-specific route generator. It reconciles installed `SKILL.md` files with the optional host catalog, excludes
unresolved duplicate names and inactive cache entries, selects the first eligible primary candidate per route, drops
routes with no eligible primary, keeps only eligible supporting and verification candidates, and writes a concrete
`routes.json`.

This module must not edit `hooks.json` or install skills. Its output should be validated with `validate_routes.py` before the hook uses it.

### `validate_routes.py`

Schema and regex validator for route config. It should catch broken route files before install or release, but it must not mutate config.

### `sync_skills.py`

Drift planner and inventory sync surface. It compares the previous manifest with the reconciled current catalog and
reports added, removed, changed, ambiguous, missing-route, and unrouted skills. `--plan` is read-only. `--apply` writes
only `skills.manifest.json`; it never edits `routes.json`, installs, removes, enables, or disables skills. Active v1/v2
bindings are resolved through the shared Policy IR. A rejected metadata path blocks strict sync only when an active route
references that skill; unused rejections are warnings.

### `doctor.py`

Read-only install health checker. It verifies installed hook files, route validation, exactly one canonical
`UserPromptSubmit` registration, the conditional `Stop` registration when measurement is enabled, real stdin smoke
behavior through standalone `python3` argv, and skill drift. Doctor always uses a temporary logging-disabled smoke
config, so its lifecycle checks do not append persistent events.

### `install.py` And `uninstall.py`

Codex home mutation surfaces. These scripts copy hook files and the bundled skill, generate user-specific route config,
update the optional visible route notice setting, or remove installed hook entries. Before mutating target paths, the
installer stages the standalone copied runtime and a logging-disabled smoke config in a temporary directory, runs a real
stdin `UserPromptSubmit` envelope through the canonical standalone `python3` argv, and cleans up staging. The implicit
smoke uses the first eligible active primary selected from Policy IR in a controlled probe route; an explicit
`--smoke-prompt` uses the validated candidate route config. Missing eligible primary fails before mutation.

Only after smoke succeeds does install snapshot all mutation targets, copy runtime and skill files, write route,
inventory, and ownership manifests, and register the hook last. A process-visible exception restores the snapshots in
reverse order. `install.manifest.json` records relative artifact paths, ownership, digest, expected registration, and a
canonical revision. The transaction snapshot directory contains a path-confined journal; the next install recovers a
matching interrupted transaction before reading current install state. Uninstall with `--remove-files` removes only
matching managed/generated artifacts and preserves modified files, symlinks, and preserved user artifacts.
Artifact path `.` and symlinked parents below the selected Codex home are unsafe: install, recovery, doctor, and uninstall
refuse to traverse them, while a leaf symlink is reported and preserved.
An unchanged bundled `personal-skill-router` with a matching managed manifest digest is upgraded automatically. Modified,
preserved, unsafe, and unowned copies remain untouched unless `--force` explicitly authorizes replacement.

### `lazy_skill_router_cli`

Small public console entrypoint for packaged installs. It exposes `install`, `doctor`, `uninstall`, `catalog`, `sync`,
`policy`, `route`, `outcome`, and `report`. Policy commands run only during explicit setup, sync, evaluation, or
promotion. The installed Codex hook remains a standalone copy and does not depend on the pipx environment.

### `.github/workflows/release.yml`

Tag-triggered release automation. It verifies tests and route fixtures, checks that the pushed `v*.*.*` tag matches
`pyproject.toml`, then builds and checks one distribution bundle. PyPI Trusted Publishing and the GitHub Release job
download that same bundle and `SHA256SUMS` under separate least-privilege permissions, so Trusted Publishing does not
share GitHub contents-write permission. This workflow must remain separate from hook runtime behavior and must not
require PyPI tokens in repository secrets.

### `eval_routes.py`

Golden prompt regression evaluator. It reads `eval/prompts.jsonl`, routes each prompt through the same core engine used by the hook, and reports expectation failures by fixture id and category.

## Data Boundaries

- Prompt text enters only through hook stdin or dry-run CLI arguments.
- Config data enters through JSON files and is normalized into Policy IR at the shared parser boundary.
- Installed skill metadata is read by `sync_skills.py` for reports and by `generate_routes.py` for user-specific config generation.
- Host catalogs contain names, descriptions, source, enabled state, and implicit-invocation state. They exclude full
  skill bodies, absolute paths, prompts, and credentials.
- Policy proposals contain synthetic positive and negative examples. They must not copy private user prompts.
- The app LLM is used only to produce draft metadata and policy proposals. Runtime matching, lifecycle enforcement,
  measurement, and promotion gates are deterministic and local.
- Optional measurement records prompt/session/turn hashes and route metadata, not raw prompt or assistant text.
- Policy feedback records a route id, proposal revision, verdict, source, and already-hashed session/turn linkage.
- Measurement is bounded by entry count and age; the defaults are 1,000 entries and 30 days.
- Outcome case ids are hashed. Outcome status is accepted only from an explicit objective, human, or grader label.
- Outcome comparisons retain policy/config revision context. Missing or mixed context is reported as non-comparable.
- Config trust is derived from discovery source and remains advisory.
- Inventory and install manifests contain relative locator references and digests, not absolute user paths.
- Optional visible route notices reveal only that the router ran, not the raw prompt, selected route, or selected skill.
- External services are never called by the hook or evaluator.

Current validation covers local macOS/POSIX behavior plus the full source-and-package workflow on Ubuntu 22.04 x64 /
Python 3.9 and Ubuntu 24.04 x64 / Python 3.11 and 3.14. Other Linux distributions and architectures remain
experimental; WSL is unverified and outside the 0.4.0 support claim, and native Windows is unsupported.

## Evaluation Strategy

Route quality is checked at three levels:

- Unit tests cover core behavior and utility scripts.
- Contract tests cover schema v2, route-result v2, structured recommendation, Hook IR, inventory, trust, retention,
  ownership drift, safe uninstall, and transaction rollback.
- `validate_routes.py` checks route JSON shape and regex validity.
- `eval_routes.py` checks golden prompt fixtures across normal routing, answer-only prompts, composite prompts, security requests, install/config requests, and external-state requests.
- Measurement tests cover inject/shadow/off delivery, session-aware lifecycle correlation, privacy fields, event schema
  handling, conflicting outcome exclusion, revision segmentation, and native/inject harm-rescue reporting.

When route behavior changes, update the golden prompt fixture in the same change. A route change without an eval update should be treated as incomplete unless the existing fixtures already cover the behavior.

## Roadmap

The remaining strategy gates are intentionally narrower than the implemented shadow contracts:

- verify actual Codex consumption before making structured recommendation the default hook wire output
- add trusted eligibility probes before promoting inventory states from `unknown`
- add explicit phase policy before emitting `mutate` or `publish`
- keep per-prompt or low-margin runtime LLM assistance disabled; app LLM use remains confined to explicit setup and sync
- add a versioned experiment manifest and objective evaluator before interpreting results across journals or corpora
- expand platform/package matrices before release claims

Any roadmap work must preserve recommendation-only semantics, fail-open behavior, and no raw prompt logging.
