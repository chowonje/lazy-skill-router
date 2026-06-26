# Architecture

## Purpose

`lazy-skill-router` is a small Codex hook project that recommends local skills for the current prompt. It helps Codex notice the right skill early, but the recommendation is advisory. The agent must still inspect the task, repository state, and higher-priority instructions before acting.

## Runtime Flow

1. Codex sends a `UserPromptSubmit` hook event to `lazy_skill_router.py`.
2. The hook adapter reads the prompt from CLI options or hook JSON on stdin.
3. `lazy_skill_router_core.py` loads route config from an explicit path, `$LAZY_SKILL_ROUTER_CONFIG`, installed Codex config, or `routes.default.json`.
4. The routing engine parses route objects, checks regex patterns, applies the skill allowlist, scores all matching candidates, and returns the highest-ranked route.
5. The adapter emits Codex hook JSON with a `<lazy-skill-router>` context block, or emits nothing when no route matches.

The hook is intentionally fail-open. Missing config, invalid JSON, invalid route entries, unreadable files, and malformed hook input should degrade to no recommendation.

## Main Modules

### `lazy_skill_router_core.py`

Pure routing engine and local metadata logging. It owns route parsing, pattern matching, confidence labels, answer-only detection, recommendation formatting, and opt-in prompt-hash logging.

The core should remain independent from Codex hook I/O so dry-run, tests, and evals can exercise routing behavior without installing the hook.

### `lazy_skill_router_scoring.py`

Candidate matching and ranking. It owns route dataclasses, confidence calculation, optional `priority` and `weight` scoring, allowlist filtering, and `fallback` handling.

Route patterns may be raw regex strings or labeled pattern objects. Matching uses the regex; recommendation context uses the label when one is provided.

Fallback routes are only selected when no non-fallback route matches. Use them for broad categories such as generic implementation work.

### `lazy_skill_router.py`

Codex hook adapter and dry-run CLI. It owns stdin parsing, CLI flags, hook JSON output, and conversion from a prompt string to either diagnostics or Codex `hookSpecificOutput`.

Dry-run diagnostics include the selected route, up to three ranked candidates, human-readable `matchedSignals`, and regex-level `matchedPatterns`. The hook output remains smaller and only injects the selected recommendation block.

### `routes.default.json`

Bundled route policy data. It defines the skill allowlist, confidence threshold, answer-only patterns, and route metadata.

Routes may define optional `priority`, `weight`, and `fallback` fields. Candidate ranking prefers non-fallback routes, then higher score, then higher confidence, then earlier config order.

### `routes.template.json`

Candidate-based route policy data for user-specific config generation. It keeps the same routing metadata as `routes.default.json`, but skill references are expressed as `primaryCandidates`, `supportingCandidates`, and `verificationCandidates`.

### `generate_routes.py`

User-specific route generator. It scans installed `SKILL.md` files, selects the first installed primary candidate per route, drops routes with no installed primary, keeps only installed supporting and verification candidates, and writes a concrete `routes.json`.

This module must not edit `hooks.json` or install skills. Its output should be validated with `validate_routes.py` before the hook uses it.

### `validate_routes.py`

Schema and regex validator for route config. It should catch broken route files before install or release, but it must not mutate config.

### `sync_skills.py`

Report-only drift detector. It compares installed `SKILL.md` metadata with `allowedSkills`, route primaries, supporting skills, and verification skills. It may exit non-zero in `--strict` mode, but it must not install, remove, or edit skills.

### `doctor.py`

Read-only install health checker. It verifies installed hook files, route validation, `UserPromptSubmit` registration, hook dry-run smoke behavior, and skill drift. It may exit non-zero when the installation is unhealthy, but it must not edit `hooks.json`, route files, skills, or hook code.

### `install.py` And `uninstall.py`

Codex home mutation surfaces. These scripts copy hook files and the bundled skill, generate user-specific route config, or remove installed hook entries. The installer validates routes and runs a hook dry-run smoke test before editing `hooks.json`; hook registration must remain the final install step. They must preserve dry-run mode, show a planned `hooks.json` diff in dry-run mode, make backups before editing `hooks.json`, and avoid broad deletion.

### `lazy_skill_router_cli`

Small public console entrypoint for packaged installs. It exposes only `install`, `doctor`, and `uninstall`, then delegates to the existing modules. When installed from a wheel, the CLI copies hook files and route templates from package data under `share/lazy-skill-router`; the installed Codex hook remains a standalone copy under `~/.codex/hooks/` and does not depend on the pipx environment at runtime.

### `.github/workflows/release.yml`

Tag-triggered release automation. It verifies that the pushed `v*.*.*` tag matches `pyproject.toml`, builds the source distribution and wheel, runs `twine check`, publishes through PyPI Trusted Publishing, then creates or updates the matching GitHub Release with `SHA256SUMS`. PyPI publishing and GitHub Release upload run in separate jobs so Trusted Publishing does not share a job with GitHub contents-write permission. This workflow must remain separate from hook runtime behavior and must not require PyPI tokens in repository secrets.

### `eval_routes.py`

Golden prompt regression evaluator. It reads `eval/prompts.jsonl`, routes each prompt through the same core engine used by the hook, and reports expectation failures by fixture id and category.

## Data Boundaries

- Prompt text enters only through hook stdin or dry-run CLI arguments.
- Config data enters through JSON files and is parsed into route objects at the core boundary.
- Installed skill metadata is read by `sync_skills.py` for reports and by `generate_routes.py` for user-specific config generation.
- Optional logging records prompt hashes and route metadata, not raw prompt text.
- External services are never called by the hook or evaluator.

## Evaluation Strategy

Route quality is checked at three levels:

- Unit tests cover core behavior and utility scripts.
- `validate_routes.py` checks route JSON shape and regex validity.
- `eval_routes.py` checks golden prompt fixtures across normal routing, answer-only prompts, composite prompts, security requests, install/config requests, and external-state requests.

When route behavior changes, update the golden prompt fixture in the same change. A route change without an eval update should be treated as incomplete unless the existing fixtures already cover the behavior.

## Roadmap

The scoring engine is still intentionally small. The next routing improvements should be made behind regression fixtures:

- expand route categories for security, install, external-state, and multi-intent prompts
- tune priority and weight values only with golden prompt coverage

Any roadmap work must preserve recommendation-only semantics, fail-open behavior, and no raw prompt logging.
