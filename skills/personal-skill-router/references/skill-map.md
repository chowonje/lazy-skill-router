# Personal Skill Map

Use this reference only when the fast routes in `SKILL.md` are not enough.

## Core Workflows

| Domain | Primary | Supporting | Notes |
|---|---|---|---|
| Thinking and planning | `superpowers` | `task-brief-normalizer`, `project-mindmap` | Use before implementation when the path is unclear. |
| Smallest correct implementation | `ponytail-lite` | task-specific primary skill | Use to reduce scope and avoid unnecessary abstraction. |
| Programming | `omo:programming` | `omo:lsp`, `verification-gate` | Default for code changes. |
| Debugging | `omo:debugging` | `omo:programming`, `verification-gate` | Default for runtime failures and wrong behavior. |
| Refactoring | `omo:refactor` | `ponytail-lite`, `omo:lsp` | Use when behavior should stay stable. |
| UI/frontend | `omo:frontend` | `omo:visual-qa`, `playwright` | Use for visual or UX work. |
| Browser automation | `playwright` | `browser:control-in-app-browser`, `chrome:control-chrome` | Use Playwright for repeatable verification. |
| GitHub context | `github:github` | `github:gh-address-comments`, `github:yeet` | Use GitHub skills only when GitHub data or actions are needed. |
| CI failure | `github:gh-fix-ci` | `github:github` | Use for GitHub Actions failures. |
| Documentation | `writing-polish` | `docs-sync`, `release-notes` | Use `docs-sync` when code and docs must match. |
| PDF | `pdf` | `writing-polish` | Use for reading, creating, or layout-sensitive PDF tasks. |
| Privacy/release preflight | `privacy-release-check` | `verification-gate` | Use before publishing, sharing, deploying, or exporting. |
| Security scan | `codex-security:security-scan` | `security-best-practices` | Use for repository or path security scans. |
| OpenAI docs | `openai-docs` | none | Use for OpenAI API, Codex, ChatGPT, and product docs. |
| Skill creation | `skill-creator` | `writing-polish` | Use when making or updating a skill. |
| Plugin creation | `plugin-creator` | `skill-creator` | Use when packaging skills, MCP, or apps as a plugin. |

## Productivity

| Task | Primary | Supporting |
|---|---|---|
| Gmail search, summary, draft, labels | `gmail:gmail` | `gmail:gmail-inbox-triage` |
| Calendar events | `google-calendar:google-calendar` | `google-calendar:google-calendar-daily-brief` |
| Meeting prep | `google-calendar:google-calendar-meeting-prep` | `google-calendar:google-calendar` |
| Drive file work | `google-drive:google-drive` | specific Docs/Sheets/Slides skill |
| Google Docs | `google-drive:google-docs` | `writing-polish` |
| Google Sheets | `google-drive:google-sheets` | none |
| Google Slides | `google-drive:google-slides` | `writing-polish` |

## Local Knowledge

| Task | Primary | Supporting |
|---|---|---|
| Find definitions, references, call paths | `code-navigation` | `omo:lsp` |
| Understand project structure | `project-mindmap` | `code-navigation` |
| Record work progress | `project-progress-log` | `writing-polish` |
| Review Codex config, hooks, MCP, skills | `agent-config-audit` | `personal-skill-router` |
| Create or update AGENTS.md | `agents-md` | `agent-config-audit` |
| API compatibility check | `api-contract-checker` | `verification-gate` |
| DB migration review | `db-migration-reviewer` | `verification-gate` |
