---
name: personal-skill-router
description: Classify the current Codex task and choose the smallest useful set of installed skills before work begins. Use when the correct skill is unclear, a request spans multiple domains, or the task involves coding, debugging, UI/frontend, browser validation, GitHub/CI, documents/PDF, Google Workspace, security, release/privacy checks, skill/plugin creation, Codex configuration, or final verification.
---

# Personal Skill Router

Use this skill as a routing layer, not as a replacement for the selected domain skill.

## Routing Rule

Before non-trivial work, classify the request into one primary domain. Pick:

- one primary skill
- up to two supporting skills
- one verification skill when work changes files, behavior, UI, docs, security posture, or external state

Do not load every related skill. Prefer the narrowest installed skill that can do the job.

## Fast Routes

| User task | Primary skill | Supporting skills |
|---|---|---|
| Requirements are unclear, options need comparing, or the user asks how to approach work | `superpowers` | `task-brief-normalizer`, `ponytail-lite` |
| Implement or edit code | `omo:programming` | `ponytail-lite`, `omo:lsp`, `verification-gate` |
| Debug a runtime failure, wrong behavior, crash, or broken UI | `omo:debugging` | `omo:programming`, `verification-gate` |
| Refactor or simplify code | `omo:refactor` | `ponytail-lite`, `omo:lsp`, `verification-gate` |
| Build or modify web UI | `omo:frontend` | `omo:visual-qa`, `playwright` |
| Verify browser behavior through clicks, forms, screenshots, or local pages | `playwright` | `browser:control-in-app-browser`, `chrome:control-chrome` |
| GitHub repo, issue, PR, or review-comment work | `github:github` | `github:gh-address-comments`, `github:yeet` |
| GitHub Actions failure | `github:gh-fix-ci` | `github:github`, `verification-gate` |
| Code review | `code-review` | `ponytail-lite` when the review is about over-engineering |
| Docs, README, release text, PR text, Korean/English prose | `writing-polish` | `docs-sync`, `release-notes` |
| PDF work | `pdf` | `writing-polish`, `verification-gate` |
| Google Drive, Docs, Sheets, Slides | `google-drive:google-drive` | specific Docs/Sheets/Slides skill |
| Gmail | `gmail:gmail` | `gmail:gmail-inbox-triage` |
| Calendar or meeting prep | `google-calendar:google-calendar` | calendar brief or meeting prep skill |
| Repository-wide security review | `codex-security:security-scan` | `security-best-practices` |
| Diff or PR security review | `codex-security:security-diff-scan` | `codex-security:triage-finding` |
| Threat model | `codex-security:threat-model` | `security-threat-model` |
| Fix a security finding | `codex-security:fix-finding` | `verification-gate` |
| Release or external sharing preflight | `privacy-release-check` | `verification-gate`, `writing-polish` |
| Project structure or subsystem understanding | `project-mindmap` | `code-navigation` |
| Codex hooks, AGENTS.md, MCP, skills, or plugin config review | `agent-config-audit` | `personal-skill-router` |
| OpenAI API, Codex, or OpenAI product docs | `openai-docs` | none by default |
| Create or update a Codex skill | `skill-creator` | `writing-polish` |
| Create a Codex plugin | `plugin-creator` | `skill-creator` |
| Install a skill | `skill-installer` | none by default |

## Overlap Policy

When two skills appear to fit, choose the more specific one:

- `superpowers` frames the work; it should hand off once a domain is clear.
- `ponytail-lite` is a guardrail, not the main implementation skill.
- `verification-gate` is for readiness checks after changes, not for initial implementation.
- `omo:visual-qa` checks visual/UI quality; `playwright` drives browser behavior.
- `github:github` reads GitHub context; `github:yeet` publishes local changes.
- `codex-security:*` skills are the primary security workflow; local `security-*` skills are lightweight supplements.

Read `references/skill-map.md` when the fast routes are not enough.
Read `references/overlap-policy.md` when multiple skills fit.

## Output When Routing

When this skill is used explicitly, answer with:

```markdown
Primary skill:
Supporting skills:
Verification skill:
Why:
Next action:
```

If the user asks to proceed, load and follow the selected primary skill before editing or acting.
