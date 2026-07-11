# lazy-skill-router

`lazy-skill-router` is a Codex hook that classifies the current prompt, separates route candidates from actual skill
activation, and can accumulate privacy-preserving activation evidence. Its app-aware sync workflow lets the host LLM
describe the skills it can actually see and propose user-specific routes before the deterministic hook evaluates them.

It is a recommendation policy, not an authorization layer. Codex should still inspect the actual task, repository
state, and safety constraints before choosing any skill.

The current shipped behavior is documented in [`CURRENT_PUBLIC_CONTRACT.md`](CURRENT_PUBLIC_CONTRACT.md).
The released compatibility baseline is `0.4.0`; unreleased source changes and rollback notes are tracked in
[`CHANGELOG.md`](CHANGELOG.md).

## Why Use It

Use this when your Codex setup has more skills than you want to remember.

`personal-skill-router` is the setup guide: it can use the current app's skill metadata to prepare a local host catalog
and policy proposal. When no compiled policy exists, it selects only from the current app catalog and abstains when no
available skill is a clear match.

`lazy-skill-router` is the hook: it reads each prompt, checks the route table, and makes a deterministic
`activate`, `propose`, or `abstain` decision. A proposal is a candidate only and does not activate a skill.

The goal is not to make skills magical. It is to stop making the user remember the whole menu.

## Not LazyCodex

This is not a replacement for LazyCodex.

LazyCodex helps orchestrate larger Codex workflows such as planning, execution, review, continuation, and multi-agent quality gates. `lazy-skill-router` is intentionally narrower: it only classifies the current prompt and recommends a small set of local skills before work starts.

Think of it as a lightweight skill menu for Codex, not a full work orchestration system.

## 한국어 소개

Codex에 스킬은 많은데, 매번 어떤 스킬을 써야 할지 떠올리는 건 번거롭습니다.

이 프로젝트는 그 과정을 두 단계로 나눕니다.

1. `personal-skill-router`는 로컬에 저장된 스킬을 용도별로 분류하는 가이드북입니다.
2. `lazy-skill-router`는 그 분류표를 `UserPromptSubmit` hook으로 연결해서, 프롬프트가 들어올 때 스킬을 바로
   활성화할지, 후보로만 제안할지, 아무 스킬도 고르지 않을지 결정합니다.

목표는 스킬을 마법처럼 자동 실행하는 것이 아니라, 사용자가 매번 스킬 메뉴를 외우지 않아도 Codex가 먼저 알맞은 후보를 떠올리게 하는 것입니다.

## LazyCodex와의 차이

이 프로젝트는 LazyCodex를 대체하려는 도구가 아닙니다.

LazyCodex가 계획, 실행, 리뷰, 이어서 작업하기, 멀티 에이전트 검증 같은 큰 작업 흐름을 다루는 시스템이라면, `lazy-skill-router`는 그보다 훨씬 좁은 문제를 다룹니다. 사용자의 현재 프롬프트를 보고 로컬에 설치된 스킬 중 어떤 것을 먼저 참고하면 좋을지 추천합니다.

즉, 작업 전체를 대신 굴리는 시스템이 아니라 Codex가 작업을 시작하기 전에 스킬 메뉴를 먼저 펼쳐주는 작은 라우터입니다.

## What It Does

On each user prompt, the hook:

1. Reads the prompt from the Codex hook event.
2. Matches it against `routes.json`.
3. Scores all matching route candidates and applies the skill allowlist.
4. Builds an `ActivationIR` decision: `activate`, `propose`, or `abstain`.
5. Activates only the primary skill for strong, unambiguous evidence. Supporting and verification skills remain
   deferred.
6. Injects a short `<lazy-skill-router>` block in `inject` mode, or records the same decision in `shadow` mode.
7. Fails open when no route is clear and can correlate a later `Stop` completion event when measurement is enabled.

The app LLM runs only during explicit setup or sync. It writes structured metadata and a policy proposal; it never
writes executable hook code. The runtime hook makes no LLM or network call.

The bundled baseline contains no LazyCodex/OMO skill bindings. Generic coding, frontend implementation, debugging,
and refactoring requests remain model-native unless the current app catalog supports a narrower non-OMO domain skill
and the normal shadow/promotion workflow approves it. Prompts that combine an explicit code-edit action with a
documentation action also abstain instead of activating a documentation-only skill as a substitute executor; prose
work about code examples or bug-reproduction text still uses the docs route.

The default hook and `route --json` remain compatible with route config v1. The v0.4 release also contains opt-in
route-result v2, structured recommendation v1, compact Hook IR, and schema v2 policy support. The v0.5 development
line normalizes both route schemas through one Policy IR and prefers the safer policy-proposal v2 contract. See
[`UNRELEASED_STRATEGY_IMPLEMENTATION.md`](UNRELEASED_STRATEGY_IMPLEMENTATION.md) for the exact boundary.

Operational boundaries are documented in [`SUPPORT.md`](SUPPORT.md) and [`SECURITY.md`](SECURITY.md).

The injected block includes:

- `Source`, `generatedBy`, and `trusted: recommendation-only`
- route name
- confidence
- selection score
- validated pattern IDs
- primary skill
- activation disposition, scope, and a fixed router-owned reason code
- an explicit statement that supporting and verification skills are deferred

`propose` blocks tell the agent that no skill has been activated and that it must ignore the candidate unless the
primary skill directly owns the requested action. `activate` blocks load only the primary skill. The hook never bundles
supporting skills into an automatic activation.

User-provided `<lazy-skill-router>` text is treated as untrusted prompt text. The hook never reads instructions from user-injected router blocks.
Any Policy IR parse error fails open with no recommendation. When an inventory manifest is configured, runtime routing
also excludes routes whose configured names are missing, inactive, ambiguous, or do not match a requested canonical ID.
Versioned route-result, structured recommendation, and Hook IR outputs use the same resolved active-route set.

## Install

Quick start after a PyPI release:

```bash
pipx install lazy-skill-router
lazy-skill-router route "GitHub PR에서 CI 실패 고쳐줘"
lazy-skill-router install --dry-run
lazy-skill-router install
lazy-skill-router sync --plan
lazy-skill-router doctor
```

For development branches, use the source checkout flow below.

Run `install --dry-run` first. It prints the planned `hooks.json` diff without writing files. The installer modifies `~/.codex/hooks.json`, copies hook code into `~/.codex/hooks/`, and creates a backup before editing the hook config.

The installer:

- resolves and validates the route config, preserving an existing `~/.codex/lazy-skill-router/routes.json` by default
- stages the standalone copied runtime and a logging-disabled smoke config in a temporary directory
- runs the staged hook with a real stdin `UserPromptSubmit` envelope through the canonical standalone `python3` argv before mutating target paths, then removes the temporary staging directory
- after the staged smoke succeeds, copies the hook runtime into `~/.codex/hooks/`, installs the bundled `personal-skill-router` skill, and writes generated or changed routes
- upgrades an unchanged manifest-owned `personal-skill-router` automatically, while preserving modified, symlinked, or user-owned copies unless `--force` is explicit
- generates path-redacted skill inventory and install ownership manifests
- journals every mutation target, restores on a later copy/write error, and recovers an interrupted transaction on the next install
- backs up `~/.codex/hooks.json` before editing it
- adds or updates one `UserPromptSubmit` hook entry
- when measurement is enabled, adds one `Stop` hook that records turn completion without storing assistant text

Hook registration is the final install step. If route generation, validation, or the staged smoke fails, the installer exits before target mutation: no new target artifacts are written, and an existing `routes.json` remains byte-for-byte unchanged.

When `--smoke-prompt` is omitted, install and doctor select the first eligible active primary from the shared Policy IR
and use it in a controlled temporary probe route. This supports both route schemas without depending on JSON order or a
shadow route. If no eligible active primary exists, install fails before target mutation. Passing `--smoke-prompt
PROMPT` instead runs that prompt against the validated real route config and requires a routed envelope; a no-match
result is a strict smoke failure.

After install, run the read-only doctor:

```bash
lazy-skill-router doctor
```

The doctor checks that hook files exist, `routes.json` validates, inventory and ownership manifest revisions are valid,
the stored inventory still matches the filesystem and optional host catalog,
managed runtime digests match, exactly one `UserPromptSubmit` router entry is registered with the canonical standalone
`python3` command, the optional `Stop` hook matches the measurement setting, the installed hook accepts real
`UserPromptSubmit` and `Stop` smoke events through a temporary logging-disabled config, and configured route skills are
installed. Missing, duplicate, or drifted registration is unhealthy. If the ownership manifest reports managed runtime
drift, doctor skips the executable smoke rather than running modified hook code.

Use a custom Codex home when needed:

```bash
lazy-skill-router install --codex-home /path/to/.codex
lazy-skill-router doctor --codex-home /path/to/.codex
```

Existing `routes.json` files are preserved by default. To regenerate routes during install:

```bash
lazy-skill-router install --overwrite-routes
```

## Upgrade

Upgrade the pipx package, then run `install` again so the standalone hook files copied into `~/.codex/hooks/` are refreshed:

```bash
pipx upgrade lazy-skill-router
lazy-skill-router install
lazy-skill-router doctor
```

If `pipx` itself is installed as a Python module but not on your shell `PATH`, use:

```bash
python3 -m pipx upgrade lazy-skill-router
```

`lazy-skill-router install` preserves an existing `~/.codex/lazy-skill-router/routes.json` by default. Use
`lazy-skill-router install --overwrite-routes` only when you want to regenerate local routes from the currently
installed skills.

The bundled `personal-skill-router` is upgraded automatically only when its previous install manifest marks it as
managed and its digest still matches. Modified, preserved, missing-manifest, symlinked, and otherwise unsafe copies are
left untouched. Use `--force` only when an explicit replacement is intended.

For the `0.4.0` upgrade, measurement remains disabled and the legacy advisory hook output remains the default. Enable
shadow measurement explicitly only after reviewing the local log path and retention settings. See
[`CHANGELOG.md`](CHANGELOG.md) for rollback and compatibility notes.

Source checkout installation is still supported:

```bash
git clone https://github.com/chowonje/lazy-skill-router.git
cd lazy-skill-router
pipx install .
lazy-skill-router install --dry-run
lazy-skill-router install
lazy-skill-router doctor
```

You can also run the source scripts directly:

```bash
python3 install.py --dry-run
python3 install.py
python3 doctor.py
```

Install only from PyPI or a trusted checkout of this repository. Avoid curl-pipe-shell installation for hook-based tools.

## Files Modified

`lazy-skill-router install` may write:

- `~/.codex/hooks/lazy_skill_router.py`
- `~/.codex/hooks/lazy_skill_router_core.py`
- `~/.codex/hooks/lazy_skill_router_common.py`
- `~/.codex/hooks/lazy_skill_router_contracts.py`
- `~/.codex/hooks/lazy_skill_router_inventory.py`
- `~/.codex/hooks/lazy_skill_router_logging.py`
- `~/.codex/hooks/lazy_skill_router_policy_ir.py`
- `~/.codex/hooks/lazy_skill_router_scoring.py`
- `~/.codex/skills/personal-skill-router/`
- `~/.codex/lazy-skill-router/routes.json`
- `~/.codex/lazy-skill-router/skills.manifest.json`
- `~/.codex/lazy-skill-router/install.manifest.json`
- `~/.codex/hooks.json`

The explicit app-policy workflow may also write user-owned `host-catalog.json`, `policy.proposal.json`,
`routes.candidate.json`, policy feedback events, and route backups. Uninstall does not remove these user-owned policy
artifacts automatically.

Complete package and bundled-skill upgrades before generating the host catalog or policy proposal. A skill-file change
after sync changes the inventory revision, so stage and promotion correctly reject the older candidate.

It does not run MCP tools, browser tools, GitHub Actions, or shell commands on your repositories.

## Uninstall

```bash
lazy-skill-router uninstall
```

To remove installed files as well as the hook entry:

```bash
lazy-skill-router uninstall --remove-files
```

The uninstall command also backs up `hooks.json` before editing it. With `--remove-files`, it removes only unchanged
artifacts covered by a valid ownership manifest. Modified files, symlinks, and preserved user files remain in place.

## Test a Prompt

Use `route` to check what the packaged CLI would recommend for a prompt:

```bash
lazy-skill-router route "GitHub PR에서 CI 실패 고쳐줘"
```

Example output:

```text
Route: github-ci
Activation: activate (eligible)
Primary skill: github:gh-fix-ci
Deferred supporting skills: github:github
Deferred verification skill: verification-gate
Confidence: 0.80 (normal)
Selection score: 0.80
Matched signals: CI keyword, Korean CI failure
Answer-only: false
```

Use `--json` when you want the full dry-run diagnostics:

```bash
lazy-skill-router route --json "GitHub PR에서 CI 실패 고쳐줘"
```

The versioned contract views, including the v0.5 development ActivationIR view, are:

```bash
lazy-skill-router route --route-result-v2 "GitHub PR에서 CI 실패 고쳐줘"
lazy-skill-router route --recommendation-json "GitHub PR에서 CI 실패 고쳐줘"
lazy-skill-router route --hook-ir-json "GitHub PR에서 CI 실패 고쳐줘"
lazy-skill-router route --activation-ir-json "GitHub PR에서 CI 실패 고쳐줘"
```

These are structured diagnostics. They do not request execution or grant permission; `--activation-ir-json` reports
the same deterministic activation decision used by the hook.

Source checkout dry-run mode is still available before enabling or tuning routes:

```bash
python3 lazy_skill_router.py --dry-run "GitHub PR에서 CI 실패 고쳐줘"
```

Example output:

```json
{
  "shouldInject": true,
  "shouldActivate": true,
  "activationDecision": "activate",
  "activationReason": "eligible",
  "route": "github-ci",
  "primary": "github:gh-fix-ci",
  "supporting": ["github:github"],
  "verification": "verification-gate",
  "confidence": 0.8,
  "score": 0.8,
  "confidenceLabel": "normal",
  "matchedSignals": ["CI keyword", "Korean CI failure"],
  "matchedPatternIds": ["github-ci.5966e187f646", "github-ci.1609409fc690"],
  "matchedPatterns": ["\\bci\\b", "ci.*실패"],
  "candidates": [
    {
      "route": "github-ci",
      "primary": "github:gh-fix-ci",
      "supporting": ["github:github"],
      "verification": "verification-gate",
      "confidence": 0.8,
      "score": 0.8,
      "confidenceLabel": "normal",
      "matchedSignals": ["CI keyword", "Korean CI failure"],
      "matchedPatternIds": ["github-ci.5966e187f646", "github-ci.1609409fc690"],
      "matchedPatterns": ["\\bci\\b", "ci.*실패"]
    }
  ],
  "answerOnly": false,
  "activation": {
    "schema": "lazy-skill-router.activation-ir/v1",
    "disposition": "activate",
    "reasonCode": "eligible",
    "scope": "turn",
    "activatedSkills": [
      {"role": "primary", "configuredName": "github:gh-fix-ci", "state": "activated"}
    ]
  }
}
```

Dry-run output includes the selected route and up to three ranked route candidates so route tuning can show why a
route won. `matchedSignals` contains human-readable labels and `matchedPatterns` preserves regexes for local diagnostics.
`matchedPatternIds` is the safe identifier list used by the model-visible hook context; labels and regexes are not
injected.

Legacy `--json` and source `--dry-run` output is operator-only tuning data. It intentionally includes regexes and
deferred skill names and must not be forwarded to an LLM or shared as a sanitized contract. Use
`--activation-ir-json`, `--recommendation-json`, or `--hook-ir-json` for path-free, prompt-free structured data.

`shouldInject` remains the compatibility field for whether model-visible candidate context will be emitted. It is not
proof that a skill was activated. `shouldActivate` and `activationDecision` carry the new activation result. Weak, ambiguous, fallback, and
answer-only matches are emitted only as `propose`; router-meta matches become `abstain` and emit no model-visible hook
context. `requestMode` reports the final `action`, `answer-only`, or `meta` classification after explicit-action and
hard-no-action rules are applied.

## Configure Routes

Runtime config resolution is `--config`, then `LAZY_SKILL_ROUTER_CONFIG`, then an existing installed config, then the bundled default. An explicit CLI or environment path is authoritative, and an existing installed config is authoritative over the bundled default. If the selected authoritative file is missing, unreadable, malformed, or has a non-object root, routing fails open with no recommendation instead of silently using a lower-precedence config. The bundled default is used only when no explicit or environment override is set and no installed config exists.

Edit:

```text
~/.codex/lazy-skill-router/routes.json
```

Important fields:

- `minConfidence`: below this value, nothing is injected
- `policyVersion`: stable policy revision reported by versioned diagnostics
- `selection.maxRecommendations`: versioned output bound, clamped to at most three
- `selection.minScoreMargin`: threshold for marking ranked results ambiguous
- `activation.mode`: `inject` (default), `shadow` (route and measure without context injection), or `off`
- `activation.autoActivateMinStrength`: minimum evidence strength for `activate`; default `0.8`
- `activation.metaPatterns`: trusted local selection-rationale regexes that force a diagnostic candidate into `abstain`
- `activation.actionPatterns`: explicit implementation/action regexes that override soft explanation wording
- `activation.noActionPatterns`: hard no-side-effect regexes that take precedence over action wording
- `defaultVerification`: used when a route omits `verification`
- `allowedSkills`: only these skills may be recommended
- `logging.enabled`: off by default
- `logging.maxEntries`: maximum retained JSONL records; default `1000`
- `logging.retentionDays`: maximum record age; default `30`
- `display.showRouterNotice`: off by default; asks Codex to briefly show that the router ran
- `routes`: route table scored as candidates
- `routes[].priority`: optional numeric score boost in `0.05` increments
- `routes[].weight`: optional direct numeric score adjustment
- `routes[].fallback`: optional boolean; fallback routes only win when no non-fallback route matches
- `routes[].patterns`: strings or `{ "id": "...", "regex": "...", "label": "...", "facet": "..." }` objects
- `routes[].activation.requiredFacets`: optional facets that must all match before automatic activation
- `routes[].activation.scope`: advisory activation lifetime: `turn`, `phase`, or `task`
- `routes[].activation.mode`: `auto` by default, or `propose-only` for routes that must never auto-activate

Schema v2 is opt-in and keeps intent/capability policy separate from concrete `skillBindings`. A binding may be a
configured-name string or `{ "skill": "...", "canonicalId": "..." }`. Runtime, validation, sync, doctor, install,
and policy compilation normalize v1 and v2 through the same Policy IR. Unsupported schema versions fail open. The
schema, example, and migration boundary are documented in
[`UNRELEASED_STRATEGY_IMPLEMENTATION.md`](UNRELEASED_STRATEGY_IMPLEMENTATION.md).

Legacy route files can reference skills that the current app no longer exposes. Use the catalog and policy sync workflow
to retire those routes explicitly; do not substitute a merely related skill or trust stale plugin-cache copies.

Use pattern labels to make local dry-run diagnostics easier to read. Labels are not included in the injected
recommendation block:

```json
{
  "patterns": [
    { "regex": "ci.*실패", "label": "Korean CI failure" }
  ]
}
```

## Show Router Usage In Replies

By default the hook is quiet: it injects routing context for Codex, but it does not ask Codex to show that context to the user.

To make active routing visible while testing, set:

```json
"display": {
  "showRouterNotice": true
}
```

in:

```text
~/.codex/lazy-skill-router/routes.json
```

Or run:

```bash
lazy-skill-router install --show-router-notice
```

When enabled, the injected context asks Codex to briefly mention a line such as:

```text
lazy-skill-router
```

before task-specific work. This is meant for testing and demos. Leave it off when you want the router to stay invisible. Use `lazy-skill-router route "..."` when you need the selected route, primary skill, and score.

To turn the notice off again:

```bash
lazy-skill-router install --hide-router-notice
```

## Generate User-Specific Routes

`routes.template.json` defines route candidates instead of one fixed skill name per route. Use `generate_routes.py` to scan installed `SKILL.md` files and write a route config that only references skills present on the current machine:

```bash
python3 generate_routes.py --dry-run
python3 generate_routes.py
```

The generator:

- reconciles filesystem skills with `host-catalog.json` when available
- excludes unresolved duplicate names and host-confirmed inactive cache entries
- selects the first eligible `primaryCandidates` entry for each route
- skips a route when none of its primary candidates are eligible
- keeps only eligible supporting and verification candidates
- writes `~/.codex/lazy-skill-router/routes.json` by default

It does not edit `hooks.json`, install skills, or change the runtime hook.

The installer runs the same generation flow automatically when `routes.json` is missing, or when `--overwrite-routes` is passed.

Validate route changes before installing them:

```bash
python3 validate_routes.py routes.default.json
python3 validate_routes.py ~/.codex/lazy-skill-router/routes.json
```

Run golden prompt regression checks after route changes:

```bash
python3 eval_routes.py eval/prompts.jsonl
```

When you add, remove, enable, or disable skills, start with a read-only sync plan:

```bash
lazy-skill-router sync --plan
lazy-skill-router sync --plan --json
```

The plan compares the current inventory with the previous manifest and reports:

- added, removed, and content-changed skills
- configured skills that are no longer installed
- route references to missing skills
- installed skills that are not yet included in the router
- duplicate installed skill names
- rejected skill metadata locators and reason codes in additive `scanIssues`
- schema-specific resolved references and canonical/availability findings

JSON reports include additive `policySchemaVersion` and `resolvedReferences`. Each resolution records the route field,
lifecycle, configured name, requested and resolved canonical IDs, and a deterministic status such as `resolved`,
`missing`, `inactive`, `ambiguous`, or `canonical_mismatch`.

`sync --plan` never edits routes or manifests. `sync --apply` updates only `skills.manifest.json`; active routes remain
byte-for-byte preserved. Duplicate names remain unavailable to automatic route generation until a host catalog resolves
the active skill.

Use `--strict` in CI or release checks when missing, inactive, ambiguous, rejected, or canonical-mismatched active
bindings should fail the command. Rejected but unreferenced metadata remains a warning. The legacy source report remains
available as `python3 sync_skills.py`.

## App LLM Policy Sync

The current app can contribute its actual visible skill names, descriptions, and enabled state without putting an LLM
inside the runtime hook. The app writes a draft; the CLI validates, sorts, and revisions it:

```bash
lazy-skill-router catalog build
lazy-skill-router catalog validate
lazy-skill-router sync --apply
lazy-skill-router policy context
```

`host-catalog.draft.json` must contain only `host`, `complete`, and skill metadata. Set `complete` to true only when the
app confirms the full runtime catalog; otherwise filesystem-only skills remain unknown rather than being falsely marked
inactive. Catalogs and inventories exclude absolute paths and full skill bodies.

`policy context` advertises `lazy-skill-router.policy-proposal/v2`. It requires identifier-safe route, intent, pattern,
facet, and configured skill names, canonical skill bindings, bounded regexes, and synthetic examples, and it has no
free-form route reason or pattern label. Optional `activation.requiredFacets` lets the app LLM require independent
signals such as `target` and `action`. Optional route mode `propose-only` keeps self-referential or advisory routes behind
agent acceptance. These fields compile into deterministic rules and never run an LLM in the hook.
Proposal v1 remains readable through a compatibility adapter and emits a deprecation warning; its identifiers pass the
same safety rules and its free-form reason and labels are discarded before any model-visible context is built.

After the app LLM writes `policy.proposal.json`, run:

```bash
lazy-skill-router policy validate
lazy-skill-router policy compile
lazy-skill-router policy stage
```

Compilation writes `routes.candidate.json`, preserves existing route records, rejects stale inventory revisions,
unavailable or ambiguous skills, unsafe regexes, and overlapping positive examples, and marks every new route `shadow`.
An explicit `retireRoutes` entry can mark a stale existing route `disabled` without deleting it. If a v2 base already
uses `allowedSkills`, compilation extends that allowlist for the new shadow route without removing existing entries.
Review the plan before staging:

```bash
lazy-skill-router policy stage --apply
lazy-skill-router install --enable-measurement
```

Shadow routes are evaluated but never injected. Explicit human or objective feedback attaches only to a real, locally
recorded shadow decision:

```bash
lazy-skill-router policy feedback --route-id pdf-generated --verdict helpful --source human
lazy-skill-router policy promote --route-id pdf-generated
```

Activation requires current inventory and host-catalog revisions, at least five same-config shadow samples where the
route would beat the active winner, helpful rate at least `0.8`, zero harmful samples, no conflicts, and explicit
approval:

```bash
lazy-skill-router policy promote --route-id pdf-generated --apply --approve
```

See [`skills/personal-skill-router/references/policy-sync.md`](skills/personal-skill-router/references/policy-sync.md)
for the draft and proposal schemas.

## Automatic Measurement And Cumulative Evaluation

Measurement is disabled by default. Enable automatic shadow decisions and turn-completion events with:

```bash
lazy-skill-router install --enable-measurement --activation-mode shadow
lazy-skill-router doctor
```

`shadow` is the native-like control mode: the router evaluates the prompt and records its decision but emits no
model-visible context. `inject` records the same decision and emits an activation or candidate-only advisory context.
`off` performs no route selection. The installer registers the `Stop` hook only while measurement is enabled, so the
default installation does not pay for an extra lifecycle process.

The equivalent config is:

```json
"logging": {
  "enabled": true,
  "path": "",
  "maxEntries": 1000,
  "retentionDays": 30
}
```

When enabled, events are written to `~/.codex/logs/lazy_skill_router.jsonl` unless `path` is set. A custom installed
Codex home uses its own `logs/` directory. Expired events and events beyond `maxEntries` are removed during the next
locked atomic write.

Decision and completion events store hashes of the prompt, session id, and turn id. They do not store the raw prompt,
raw ids, assistant response, transcript path, or working directory:

```json
{"schema":"lazy-skill-router.measurement-event/v1","eventType":"decision","mode":"shadow","route":"github-ci","activationDisposition":"activate","injected":false,"turnHash":"...","promptHash":"..."}
```

A `completion` event means the Codex turn stopped; it is not a success claim. Record success only from an objective
check, a human review, or an explicit grader:

```bash
config="${CODEX_HOME:-$HOME/.codex}/lazy-skill-router/routes.json"
lazy-skill-router outcome \
  --config "$config" \
  --case-id fix-ci-001 --replicate 1 \
  --arm native --status fail --source objective
lazy-skill-router outcome \
  --config "$config" \
  --case-id fix-ci-001 --replicate 1 \
  --arm inject --status pass --source objective
```

Case ids are stored only as hashes. Use a sanitized stable id rather than private task text. Pass the same `--config`
for every arm so the outcome records carry the policy and config revisions used by the experiment. An outcome written
without `--config` is retained as unversioned evidence and is not claimed as an aggregate-comparable result. Summarize
all accumulated events with:

```bash
lazy-skill-router report --config "$config"
lazy-skill-router report --config "$config" --json
```

The versioned report includes route counts, activation/proposal/abstention counts and rates, injection/shadow counts,
internal decision-latency mean/p95/max, correlated completion rate, success by experiment arm, and paired native/inject
rescue, harm, and net-win counts. `decisions.shadowOnly` counts prompts matched only by lifecycle-shadow routes and
includes them in the delivery abstention rate. Completion correlation requires both the session and turn hashes. Turn-based outcomes without a case id also
require both identifiers. Duplicate same-status outcomes are counted once; conflicting labels are excluded from success
and pair metrics. Unknown event schemas with a valid timestamp are preserved in the bounded journal but ignored by the current report. Mixed
revisions, unversioned outcomes, conflicts, invalid outcomes, and ignored events are exposed
as warnings, and the report marks the aggregate as non-comparable when appropriate. Decision latency excludes Python
process startup. Automatic hooks supply delivery and completion evidence; they cannot infer task success without an
outcome label.

Until a versioned experiment manifest exists, use one journal per fixed corpus/config experiment. Reusing one journal
across unrelated experiments can still make case identity and aggregate interpretation ambiguous even though revision
mixing is detected.

## Safety Notes

- This hook fails open: malformed input or invalid config results in no injection.
- The installer modifies `~/.codex/hooks.json`; run `lazy-skill-router install --dry-run` before installing.
- `lazy-skill-router doctor` is read-only, uses a temporary logging-disabled route config for hook smoke checks, skips executable smoke after managed runtime drift, and exits non-zero when the installed hook, routes, manifests, registration, or configured skills are unhealthy.
- Config source trust, route rank, and inventory availability are advisory and never authorize execution.
- Inventory resolution rejects canonical IDs shared by multiple usable configured names, and unresolved default
  verification skills are omitted from hook context.
- The installer restores snapshotted targets on mutation errors and replays a path-confined recovery journal on the next install after interruption.
- Installer dry-run only validates and reports pending recovery; it does not restore snapshots or delete journals.
- Hook replacement and removal use exact normalized commands from the canonical install or a valid confined ownership
  manifest. A command that merely contains `lazy_skill_router.py` is preserved as foreign.
- Install/recovery/uninstall reject symlinked artifact parents below the selected Codex home; `uninstall --remove-files`
  preserves modified files and leaf symlinks instead of following or deleting them.
- Uninstall refuses a symlinked `hooks.json` write target.
- Policy stage/promotion and host-catalog build refuse leaf symlinks and symlinked parent components before atomic writes.
- The installer backs up `hooks.json` before editing it.
- Install only from PyPI or a trusted checkout of this repository.
- It does not read secrets or authentication files.
- `sync_skills.py` bounds frontmatter reads to 64 KiB/200 lines, streams digests up to a 1 MiB ceiling, escapes terminal
  controls in human reports, and does not edit hook or route configuration.
- It does not execute MCP tools, browser tools, GitHub actions, or shell commands.
- It only writes measurement events when logging is explicitly enabled; `outcome` writes only when explicitly invoked.
- It never commits, pushes, installs plugins, or changes repositories.
- Current validation covers local macOS/POSIX, an isolated Codex CLI 0.144.0 shadow-hook canary, a Python 3.9
  v0.3/v0.4 rollback canary, Ubuntu 22.04 x64 with Python 3.9, and Ubuntu 24.04 x64 with Python 3.11 and 3.14. Other
  Linux distributions and architectures remain experimental; WSL is unverified and outside the 0.4.0 support claim,
  and native Windows is unsupported.

## Release Checksums

For public releases, generate a checksum manifest and attach it to the GitHub release:

```bash
python3 release_checksums.py --output SHA256SUMS
python3 release_checksums.py --verify SHA256SUMS
```

Verification rejects empty, partial, duplicate, absolute, parent-traversing, and symlinked entries. The manifest must
exactly cover the selected artifact root. Checksums do not replace reviewing the source before installation, but they
help users confirm that release files match the published manifest.

If you publish signed releases, sign the checksum manifest rather than individual files:

```bash
gpg --detach-sign --armor SHA256SUMS
gpg --verify SHA256SUMS.asc SHA256SUMS
```

## PyPI Release

PyPI publishing uses GitHub Actions Trusted Publishing. Configure the PyPI project trusted publisher before pushing a release tag:

- PyPI project: `lazy-skill-router`
- Owner: `chowonje`
- Repository: `lazy-skill-router`
- Workflow: `release.yml`
- Environment: `pypi`

The release workflow verifies the tests and route fixtures, confirms that the Git tag matches `pyproject.toml`, builds
the source distribution and wheel once, and checks the resulting bundle with `twine` and `SHA256SUMS`. The exact same
bundle is then published to PyPI and attached to the matching GitHub Release. PyPI publishing and GitHub Release upload
run in separate jobs so Trusted Publishing does not share GitHub contents-write permission. The workflow does not store
a PyPI token in GitHub secrets.

Release steps:

```bash
version="$(awk -F'"' '/^version = / {print $2; exit}' pyproject.toml)"
git tag "v$version"
git push origin "v$version"
```

After the workflow succeeds, users can install with:

```bash
pipx install lazy-skill-router
lazy-skill-router route "GitHub PR에서 CI 실패 고쳐줘"
lazy-skill-router install --dry-run
lazy-skill-router install
lazy-skill-router doctor
```

## Development

Run the tests:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile lazy_skill_router.py lazy_skill_router_activation.py lazy_skill_router_contracts.py lazy_skill_router_core.py lazy_skill_router_common.py lazy_skill_router_host_catalog.py lazy_skill_router_install_manifest.py lazy_skill_router_inventory.py lazy_skill_router_logging.py lazy_skill_router_policy.py lazy_skill_router_policy_ir.py lazy_skill_router_scoring.py measurement.py lazy_skill_router_cli/cli.py generate_routes.py install.py doctor.py uninstall.py validate_routes.py release_checksums.py sync_skills.py eval_routes.py
python3 -m json.tool routes.default.json >/dev/null
python3 -m json.tool routes.template.json >/dev/null
python3 validate_routes.py routes.default.json
python3 eval_routes.py eval/prompts.jsonl
python3 generate_routes.py --dry-run
python3 sync_skills.py --routes routes.default.json --manifest-output /tmp/lazy-skill-router-skills.json --strict
tmp="$(mktemp -d)"
python3 install.py --codex-home "$tmp/codex" --agents-home "$tmp/agents" --dry-run
python3 install.py --codex-home "$tmp/codex" --agents-home "$tmp/agents"
python3 doctor.py --codex-home "$tmp/codex" --agents-home "$tmp/agents"
python3 -m build
python3 -m twine check dist/*
pipx_home="$(mktemp -d)"
pipx_bin="$(mktemp -d)"
PIPX_HOME="$pipx_home" PIPX_BIN_DIR="$pipx_bin" python3 -m pipx install dist/*.whl
"$pipx_bin/lazy-skill-router" install --codex-home "$tmp/codex-wheel" --agents-home "$tmp/agents-wheel" --dry-run
"$pipx_bin/lazy-skill-router" install --codex-home "$tmp/codex-wheel" --agents-home "$tmp/agents-wheel"
"$pipx_bin/lazy-skill-router" doctor --codex-home "$tmp/codex-wheel" --agents-home "$tmp/agents-wheel"
ruff check .
```

GitHub Actions also builds a temporary skill fixture, generates routes from `routes.template.json`, and then runs `sync_skills.py --strict` against the generated config so validation does not depend on the runner having local Codex skills installed.

## License

MIT
