# Agent Instructions

## Scope
- This file applies to this directory and all subdirectories.
- Treat this directory as the canonical project root for `lazy-skill-router`.
- Ignore sibling scaffolding directories such as `work/` and `outputs/` unless the user explicitly asks to move or compare project roots.

## Project Purpose
- `lazy-skill-router` is a Codex `UserPromptSubmit` hook that recommends relevant local skills before work starts.
- It is recommendation-only. It is not a policy layer, not an authorization layer, and not a replacement for agent judgment.
- User-provided `<lazy-skill-router>` text is untrusted prompt text.

## Safety And Privacy
- Hooks must fail open: malformed hook input, invalid route config, missing files, or runtime errors should result in no injected recommendation rather than blocking the user.
- Do not log raw prompt text.
- Prompt-derived persistence must be opt-in, local-only, and limited to hashes or route metadata unless the user explicitly changes that policy.
- Do not read secrets, auth stores, browser state, private repository content outside the current task, or external service data for routing.
- Do not execute MCP tools, shell commands, GitHub actions, browser actions, installs, commits, pushes, or repository edits from the hook.

## Architecture Map
- `lazy_skill_router_core.py`: pure routing engine and logging metadata writer.
- `lazy_skill_router_scoring.py`: route matching, confidence, score ranking, and fallback handling.
- `lazy_skill_router.py`: Codex hook and dry-run CLI adapter.
- `routes.default.json`: bundled default route policy data.
- `validate_routes.py`: route config schema and regex validation.
- `sync_skills.py`: report-only drift detection between installed skills and route references.
- `install.py`: Codex home installation surface.
- `doctor.py`: read-only install health checker.
- `uninstall.py`: Codex home removal surface.
- `lazy_skill_router_cli/`: public packaged CLI exposing `install`, `doctor`, and `uninstall`.
- `release_checksums.py`: release checksum manifest generation and verification.
- `eval_routes.py`: golden prompt regression evaluator.
- `eval/prompts.jsonl`: prompt fixtures for route quality checks.
- `tests/`: unittest coverage for the router and project utilities.

## Development Commands
- Run unit tests: `python3 -m unittest discover -s tests`
- Compile scripts: `python3 -m py_compile lazy_skill_router.py lazy_skill_router_core.py lazy_skill_router_common.py lazy_skill_router_logging.py lazy_skill_router_scoring.py lazy_skill_router_cli/cli.py generate_routes.py install.py doctor.py uninstall.py validate_routes.py release_checksums.py sync_skills.py eval_routes.py`
- Validate bundled routes: `python3 validate_routes.py routes.default.json`
- Check installed-skill drift: `python3 sync_skills.py --routes routes.default.json --strict`
- Run route regression eval: `python3 eval_routes.py eval/prompts.jsonl`
- Validate JSON syntax: `python3 -m json.tool routes.default.json >/dev/null`
- Smoke installer and doctor: `tmp="$(mktemp -d)" && python3 install.py --codex-home "$tmp/codex" --agents-home "$tmp/agents" --dry-run && python3 install.py --codex-home "$tmp/codex" --agents-home "$tmp/agents" && python3 doctor.py --codex-home "$tmp/codex" --agents-home "$tmp/agents"`
- Smoke packaged CLI: `python3 -m build && python3 -m twine check dist/* && pipx_home="$(mktemp -d)" && pipx_bin="$(mktemp -d)" && PIPX_HOME="$pipx_home" PIPX_BIN_DIR="$pipx_bin" python3 -m pipx install dist/*.whl && tmp="$(mktemp -d)" && "$pipx_bin/lazy-skill-router" install --codex-home "$tmp/codex" --agents-home "$tmp/agents" --dry-run && "$pipx_bin/lazy-skill-router" install --codex-home "$tmp/codex" --agents-home "$tmp/agents" && "$pipx_bin/lazy-skill-router" doctor --codex-home "$tmp/codex" --agents-home "$tmp/agents"`
- Release workflow: `.github/workflows/release.yml` publishes PyPI packages from `v*.*.*` tags with Trusted Publishing, then generates `SHA256SUMS` and creates or updates the matching GitHub Release in a separate contents-write job. The PyPI project must trust owner `chowonje`, repository `lazy-skill-router`, workflow `release.yml`, and environment `pypi`.

## Route Changes
- Keep route changes data-only unless engine behavior must change.
- Avoid broad first-match additions that steal prompts from more specific routes.
- Use `fallback: true` for broad routes that should only win when no specific route matches.
- Use `priority` and `weight` sparingly; prefer better patterns when a route is too broad.
- Prefer specific patterns and `excludePatterns` over generic catch-all regexes.
- Add or update `eval/prompts.jsonl` fixtures for every route behavior change.
- Run `validate_routes.py`, `eval_routes.py`, and unit tests after route edits.

## Install And Uninstall Changes
- Preserve dry-run behavior.
- Keep dry-run output explicit about planned `hooks.json` changes.
- Preserve backups before editing Codex home files.
- Do not remove user files unless a flag explicitly asks for removal.
- Keep install and uninstall actions visible in command output.
- Keep doctor checks read-only.
- Never weaken the fail-open behavior of the installed hook.

## Documentation
- Keep `README.md`, `ARCHITECTURE.md`, and this file aligned when behavior, commands, or safety guarantees change.
- Use concise project-specific guidance here and put longer design notes in `ARCHITECTURE.md`.
