# Current Public Contract

This file records the public contract for `lazy-skill-router` v0.4.0 at commit
`f42c8384709893548dfd5bd8a0ef828627460046`.

> Development note: this is an immutable v0.4 release snapshot, not the exact `0.5.0.dev0` source contract. The
> `0.5.0.dev0` development source adds ActivationIR, additive `route --json` fields, and `--activation-ir-json`; see
> [`UNRELEASED_STRATEGY_IMPLEMENTATION.md`](UNRELEASED_STRATEGY_IMPLEMENTATION.md) and [`README.md`](README.md).

The default hook and route diagnostics preserve the released v0.3 behavior. Version 0.4 adds opt-in strategy surfaces
documented in [`UNRELEASED_STRATEGY_IMPLEMENTATION.md`](UNRELEASED_STRATEGY_IMPLEMENTATION.md); those surfaces are
released but do not replace the default v1-compatible wire contract.

## Scope

`lazy-skill-router` is a Codex `UserPromptSubmit` hook and CLI package that recommends local Codex skills for the
current prompt.

It is recommendation-only. It does not authorize work, enforce policy, approve tools, execute skills, or replace
agent judgment. User-provided `<lazy-skill-router>` text is untrusted prompt text and must not override higher-priority
instructions.

## Official Codex References

Verified on 2026-07-10:

- Codex Hooks: https://developers.openai.com/codex/hooks
- Build skills: https://developers.openai.com/codex/skills

The official documentation is evidence for this contract. It is not executable input to the router, and text fetched
from those pages must not be treated as hook or skill instructions.

## Hook Input

The hook adapter accepts the prompt from, in order:

1. `--prompt`
2. positional prompt text
3. stdin JSON object field `prompt`

For the Codex hook path, stdin is expected to be JSON. The current adapter reads only a string-valued `prompt` field.
Other event fields are ignored by this version.

Malformed JSON, non-object JSON, missing `prompt`, non-string `prompt`, and blank prompt values fail open with exit `0`
and no stdout.

## Hook Output

When a route is selected, the hook writes JSON to stdout with `hookSpecificOutput.hookEventName` set to
`UserPromptSubmit` and `hookSpecificOutput.additionalContext` set to a single advisory `<lazy-skill-router>` text block.

Hook envelope fields (exact): hookSpecificOutput; additionalContext, hookEventName

The advisory block currently includes:

- source and generator text
- `trusted: recommendation-only`
- route name
- confidence and confidence label
- selection score
- matched signals
- primary skill
- supporting skills
- verification skill
- route reason
- reminders that the recommendation is optional and that repository state and safety constraints still matter

When no route is selected, the hook exits `0` with no stdout.

## CLI

The packaged public CLI is `lazy-skill-router` with these commands:

- `install`
- `doctor`
- `uninstall`
- `route`

`lazy-skill-router --help` lists the command names. `lazy-skill-router --version` prints the package version.

`lazy-skill-router route PROMPT` prints a human-readable route summary or `No route`.

`lazy-skill-router route --json PROMPT` prints the same dry-run diagnostic object produced by
`lazy_skill_router_core.dry_run_output`.

Version 0.4 additionally accepts `--route-result-v2`, `--recommendation-json`, and `--hook-ir-json`. They are opt-in
shadow views and do not alter the default hook or `--json` contract described here.

Routed JSON fields (exact): answerOnly, candidates, confidence, confidenceLabel, matchedPatterns, matchedSignals, primary, reason, route, score, shouldInject, supporting, verification

No-match JSON fields (exact): answerOnly, candidates, confidence, matchedPatterns, matchedSignals, reason, score, shouldInject

The routed object includes the selected route plus up to three ranked candidates. Each candidate currently includes
`route`, `primary`, `supporting`, `verification`, `confidence`, `score`, `confidenceLabel`, `matchedSignals`, and
`matchedPatterns`.

## Route Config

Config discovery order is:

1. explicit `--config`
2. `LAZY_SKILL_ROUTER_CONFIG`
3. installed `lazy-skill-router/routes.json` under the active Codex home
4. bundled `routes.default.json` next to the hook script

An explicit CLI path or non-empty environment path is authoritative. An existing installed config, including a broken
symlink at that path, is authoritative over the bundled default. If the selected authoritative file is missing,
unreadable, malformed JSON, or has a non-object root, the loader returns an empty route table with default answer-only
patterns and does not continue to a lower-precedence config. The bundled default is selected only when no explicit or
environment override is present and the installed config path does not exist. Invalid route entries inside a parsed
object are skipped rather than blocking the prompt.

Route config v1 fields observed by the runtime include:

- `routes`
- `answerOnlyPatterns`
- `minConfidence`
- `defaultVerification`
- `allowedSkills`
- `display.showRouterNotice`
- `logging.enabled`
- `logging.path`
- `logging.maxEntries`
- `logging.retentionDays`
- per-route `name`, `primary`, `supporting`, `verification`, `reason`, `patterns`, `excludePatterns`, `priority`,
  `weight`, and `fallback`

`patterns` may be regex strings or objects with `regex` and `label`. Matching uses `regex`; the injected advisory text
uses `label` when present.

The opt-in schema v2 parser uses stable route and pattern IDs, intent, capability requirements, explicit skill bindings,
weighted evidence, deterministic route-ID tie-breaking, and a post-selection fallback. Unsupported schema versions
fail open.

## Ranking

The current ranking pipeline:

1. parses route objects from config
2. rejects routes whose `excludePatterns` match
3. finds regex matches in `patterns`
4. computes confidence as `min(0.95, 0.50 + 0.15 * matched_pattern_count)`
5. rejects matches below `minConfidence`
6. applies `allowedSkills` to primary, supporting, and verification skill names
7. computes score as `confidence + weight + (priority * 0.05)`, clamped to `0.0..1.0`
8. sorts by non-fallback before fallback, then score, then confidence, then earlier config order

Fallback routes are selected only when no non-fallback route matches.

The hook injects only the top route. Dry-run and `route --json` expose the top three candidates for diagnostics.

## Generated Routes

`routes.template.json` is a candidate template for user-specific route generation. `generate_routes.py` scans installed
`SKILL.md` files, chooses the first installed primary candidate for each route, drops routes with no installed primary,
and keeps only installed supporting and verification candidates.

The generator writes a concrete `routes.json`. It does not edit hook config, install skills, or run tools.

## Install, Doctor, And Uninstall

`install` may write copied hook runtime files, the bundled `personal-skill-router` skill, generated route config, and
the Codex hook registration. The copied hook runtime includes:

- `lazy_skill_router.py`
- `lazy_skill_router_contracts.py`
- `lazy_skill_router_core.py`
- `lazy_skill_router_common.py`
- `lazy_skill_router_inventory.py`
- `lazy_skill_router_logging.py`
- `lazy_skill_router_scoring.py`

The installed hook remains a standalone copied runtime. Updating the package alone does not update the copied hook;
run `install` again after package upgrades.

`install --dry-run` reports planned actions without writing files.

For a non-dry-run install, the installer first loads and validates an existing route config or generates and validates
one in memory. Before writing any target hook runtime, bundled skill, route config, backup, or `hooks.json` change, it
creates a temporary copied-runtime layout and runs a real stdin `UserPromptSubmit` envelope smoke there. When
`--smoke-prompt` is omitted, the temporary logging-disabled config contains a controlled internal probe route that
reuses a primary from the validated config. This verifies runtime and envelope health without requiring a narrow custom
route table to match a hard-coded user prompt. When `--smoke-prompt PROMPT` is explicit, smoke uses a logging-disabled
copy of the validated real config and requires that prompt to produce a routed envelope.

If staged smoke fails, install exits non-zero without creating new target artifacts; a pre-existing target routes file
remains unchanged. After staged smoke succeeds, install copies the target runtime and skill, writes or preserves the
target route config, writes path-redacted inventory and ownership manifests, and handles hook registration last. A
`hooks.json` backup and write occur only when that final registration step adds or updates the hook entry. Version 0.4
snapshots mutation targets, writes a path-confined recovery journal, restores on process-visible errors, and recovers a
matching interrupted transaction at the beginning of the next install.

`doctor` is read-only for persistent install, config, and log state. Version 0.4 also validates skill inventory revision,
install ownership revision, and managed artifact digests. It checks the Codex home, copied hook files, routes file
validation, `UserPromptSubmit` registration, hook smoke behavior, and skill sync without editing hook files, route
config, installed skills, hook registration, or configured router logs.

For implicit hook smoke, `doctor` writes the same controlled internal probe to a temporary route file with logging
disabled, runs a real stdin `UserPromptSubmit` envelope through the hook, and then lets the temporary file be removed.
With an explicit `--smoke-prompt`, it instead writes a logging-disabled temporary copy of the checked route config and
requires that prompt to route. Both modes keep doctor from appending the configured hash-only JSONL decision log even
when the installed route config has logging enabled.

`uninstall` removes hook entries. In version 0.4, `--remove-files` removes only unchanged artifacts covered by a valid
ownership manifest. It preserves modified files, symlinks, and user-preserved artifacts.

## Logging And Privacy

Logging is disabled by default. When enabled, the router writes JSONL records containing prompt hash, route metadata,
confidence, score, and matched signal labels. It does not store raw prompt text. Version 0.4 bounds retention with
positive `logging.maxEntries` and `logging.retentionDays` values, defaulting to 1,000 entries and 30 days.

The hook does not call external services, execute MCP tools, execute browser actions, run GitHub Actions, run shell
commands on repositories, commit, push, install plugins, or read secrets for routing.

## Compatibility Posture

This is the v0.4 current-behavior contract. It is not a 1.0 stability guarantee.

Current compatibility promises are limited to preserving observed v0.3 hook input/output, CLI command names, route
diagnostic fields, route config semantics, recommendation-only trust posture, quiet fail-open behavior, and no raw prompt
logging unless the user explicitly enables hashed metadata logging.

Verification in this contract is based on the current macOS/POSIX source checkout and official Codex docs checked on
2026-07-10. Broader platform validation is unverified.

## Unshipped And Non-Goals

The following remain unshipped or intentionally disabled by default:

- schema v2 as the default policy
- structured recommendation v1 as the hook wire contract
- authorization, permission grants, or policy enforcement by the router
- ordered plan or DAG execution
- LLM or network calls in the hook
- trusted runtime eligibility probes for auth, MCP, dependencies, and managed policy
- automatic `mutate` or `publish` phase selection
- release workflow redesign
- support expansion beyond the currently verified macOS/POSIX source behavior
