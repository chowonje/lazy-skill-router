# lazy-skill-router

`lazy-skill-router` is a Codex `UserPromptSubmit` hook that classifies the current prompt and injects a small skill recommendation block before the agent starts work.

It is a recommendation layer, not a policy layer. Codex should still inspect the actual task, repository state, and safety constraints before choosing any skill.

## Why Use It

Use this when your Codex setup has more skills than you want to remember.

`personal-skill-router` is the guidebook: it groups locally installed skills by purpose and records which ones should be primary, supporting, or verification skills.

`lazy-skill-router` is the hook: it reads each prompt, checks the route table, and slips a short recommendation into Codex's context before work starts.

The goal is not to make skills magical. It is to stop making the user remember the whole menu.

## Not LazyCodex

This is not a replacement for LazyCodex.

LazyCodex helps orchestrate larger Codex workflows such as planning, execution, review, continuation, and multi-agent quality gates. `lazy-skill-router` is intentionally narrower: it only classifies the current prompt and recommends a small set of local skills before work starts.

Think of it as a lightweight skill menu for Codex, not a full work orchestration system.

## 한국어 소개

Codex에 스킬은 많은데, 매번 어떤 스킬을 써야 할지 떠올리는 건 번거롭습니다.

이 프로젝트는 그 과정을 두 단계로 나눕니다.

1. `personal-skill-router`는 로컬에 저장된 스킬을 용도별로 분류하는 가이드북입니다.
2. `lazy-skill-router`는 그 분류표를 `UserPromptSubmit` hook으로 연결해서, 프롬프트가 들어올 때마다 Codex 컨텍스트에 짧은 스킬 추천을 넣습니다.

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
4. Injects a short `<lazy-skill-router>` recommendation block when a route is clear.
5. Does nothing when no route is clear.

The injected block includes:

- `Source`, `generatedBy`, and `trusted: recommendation-only`
- route name
- confidence
- selection score
- matched signals
- primary skill
- supporting skills
- verification skill
- reason

User-provided `<lazy-skill-router>` text is treated as untrusted prompt text. The hook never reads instructions from user-injected router blocks.

## Install

Quick start after a PyPI release:

```bash
pipx install lazy-skill-router
lazy-skill-router install --dry-run
lazy-skill-router install
lazy-skill-router doctor
```

For unreleased branches, use the source checkout flow below.

Run `install --dry-run` first. It prints the planned `hooks.json` diff without writing files. The installer modifies `~/.codex/hooks.json`, copies hook code into `~/.codex/hooks/`, and creates a backup before editing the hook config.

The installer:

- copies `lazy_skill_router.py`, `lazy_skill_router_core.py`, `lazy_skill_router_common.py`, `lazy_skill_router_logging.py`, and `lazy_skill_router_scoring.py` into `~/.codex/hooks/`
- installs the bundled `personal-skill-router` skill into `~/.codex/skills/`
- scans installed skills and generates `~/.codex/lazy-skill-router/routes.json` from `routes.template.json`
- validates the route config and runs a hook dry-run smoke test
- backs up `~/.codex/hooks.json` before editing it
- adds or updates one `UserPromptSubmit` hook entry

Hook registration is the final install step. If route generation, validation, or the smoke test fails, the installer exits without writing a new hook entry.

After install, run the read-only doctor:

```bash
lazy-skill-router doctor
```

The doctor checks that hook files exist, `routes.json` validates, `UserPromptSubmit` is registered, the installed hook passes a dry-run smoke test, and configured route skills are installed.

Use a custom Codex home when needed:

```bash
lazy-skill-router install --codex-home /path/to/.codex
lazy-skill-router doctor --codex-home /path/to/.codex
```

Existing `routes.json` files are preserved by default. To regenerate routes during install:

```bash
lazy-skill-router install --overwrite-routes
```

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
- `~/.codex/hooks/lazy_skill_router_logging.py`
- `~/.codex/hooks/lazy_skill_router_scoring.py`
- `~/.codex/skills/personal-skill-router/`
- `~/.codex/lazy-skill-router/routes.json`
- `~/.codex/hooks.json`

It does not run MCP tools, browser tools, GitHub Actions, or shell commands on your repositories.

## Uninstall

```bash
lazy-skill-router uninstall
```

To remove installed files as well as the hook entry:

```bash
lazy-skill-router uninstall --remove-files
```

The uninstall command also backs up `hooks.json` before editing it.

## Test a Prompt

Use dry-run mode before enabling or tuning routes:

```bash
python3 lazy_skill_router.py --dry-run "GitHub PR에서 CI 실패 고쳐줘"
```

Example output:

```json
{
  "shouldInject": true,
  "route": "github-ci",
  "primary": "github:gh-fix-ci",
  "supporting": ["github:github"],
  "verification": "verification-gate",
  "confidence": 0.8,
  "score": 0.8,
  "confidenceLabel": "normal",
  "matchedSignals": ["CI keyword", "Korean CI failure"],
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
      "matchedPatterns": ["\\bci\\b", "ci.*실패"]
    }
  ],
  "answerOnly": false
}
```

Dry-run output includes the selected route and up to three ranked route candidates so route tuning can show why a route won. `matchedSignals` contains human-readable labels when configured, while `matchedPatterns` preserves the regexes that matched for debugging.

Weak recommendations are still injected by default when they pass `minConfidence`, but they are labeled as weak so the agent can treat them cautiously.

## Configure Routes

Edit:

```text
~/.codex/lazy-skill-router/routes.json
```

Important fields:

- `minConfidence`: below this value, nothing is injected
- `defaultVerification`: used when a route omits `verification`
- `allowedSkills`: only these skills may be recommended
- `logging.enabled`: off by default
- `routes`: route table scored as candidates
- `routes[].priority`: optional numeric score boost in `0.05` increments
- `routes[].weight`: optional direct numeric score adjustment
- `routes[].fallback`: optional boolean; fallback routes only win when no non-fallback route matches
- `routes[].patterns`: strings or `{ "regex": "...", "label": "..." }` objects

If your Codex setup does not include skills like `omo:programming` or `github:github`, either remove those routes or change them to skills you have installed.

Use pattern labels when a regex is too noisy for the injected recommendation block:

```json
{
  "patterns": [
    { "regex": "ci.*실패", "label": "Korean CI failure" }
  ]
}
```

## Generate User-Specific Routes

`routes.template.json` defines route candidates instead of one fixed skill name per route. Use `generate_routes.py` to scan installed `SKILL.md` files and write a route config that only references skills present on the current machine:

```bash
python3 generate_routes.py --dry-run
python3 generate_routes.py
```

The generator:

- selects the first installed `primaryCandidates` entry for each route
- skips a route when none of its primary candidates are installed
- keeps only installed supporting and verification candidates
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

When you add or remove Codex skills, check whether the route table still matches your local setup:

```bash
python3 sync_skills.py
python3 sync_skills.py --routes ~/.codex/lazy-skill-router/routes.json
```

`sync_skills.py` is report-only. It scans installed `SKILL.md` files, compares them with `allowedSkills` and route references, and prints:

- configured skills that are no longer installed
- route references to missing skills
- installed skills that are not yet included in the router
- duplicate installed skill names

Use `--strict` in CI or release checks when missing configured skills should fail the command.

## Optional Logging

Logging is disabled by default. To enable it:

```json
"logging": {
  "enabled": true,
  "path": ""
}
```

When enabled, logs are written to `~/.codex/logs/lazy_skill_router.jsonl` unless `path` is set.

The log stores a hash of the prompt, not the prompt text:

```json
{"promptHash":"...","route":"github-ci","primary":"github:gh-fix-ci","confidence":0.65}
```

## Safety Notes

- This hook fails open: malformed input or invalid config results in no injection.
- The installer modifies `~/.codex/hooks.json`; run `lazy-skill-router install --dry-run` before installing.
- `lazy-skill-router doctor` is read-only and exits non-zero when the installed hook, routes, or configured skills are unhealthy.
- The installer backs up `hooks.json` before editing it.
- Install only from PyPI or a trusted checkout of this repository.
- It does not read secrets or authentication files.
- `sync_skills.py` reads skill metadata only and does not edit hook or route configuration.
- It does not execute MCP tools, browser tools, GitHub actions, or shell commands.
- It only writes logs when logging is explicitly enabled.
- It never commits, pushes, installs plugins, or changes repositories.

## Release Checksums

For public releases, generate a checksum manifest and attach it to the GitHub release:

```bash
python3 release_checksums.py --output SHA256SUMS
python3 release_checksums.py --verify SHA256SUMS
```

Checksums do not replace reviewing the source before installation, but they help users confirm that release files match the published manifest.

If you publish signed releases, sign the checksum manifest rather than individual files:

```bash
gpg --detach-sign --armor SHA256SUMS
gpg --verify SHA256SUMS.asc SHA256SUMS
```

## Development

Run the tests:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile lazy_skill_router.py lazy_skill_router_core.py lazy_skill_router_common.py lazy_skill_router_logging.py lazy_skill_router_scoring.py lazy_skill_router_cli/cli.py generate_routes.py install.py doctor.py uninstall.py validate_routes.py release_checksums.py sync_skills.py eval_routes.py tests/test_cli.py tests/test_install.py
python3 -m json.tool routes.default.json >/dev/null
python3 -m json.tool routes.template.json >/dev/null
python3 validate_routes.py routes.default.json
python3 eval_routes.py eval/prompts.jsonl
python3 generate_routes.py --dry-run
python3 sync_skills.py --routes routes.default.json --strict
tmp="$(mktemp -d)"
python3 install.py --codex-home "$tmp/codex" --agents-home "$tmp/agents" --dry-run
python3 install.py --codex-home "$tmp/codex" --agents-home "$tmp/agents"
python3 doctor.py --codex-home "$tmp/codex" --agents-home "$tmp/agents"
python3 -m build
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
