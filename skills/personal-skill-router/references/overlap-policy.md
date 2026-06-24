# Overlap Policy

Primary skills own the work. Supporting skills constrain, inspect, or verify.

## Common Overlaps

| Overlap | Choose | Why |
|---|---|---|
| `superpowers` vs `task-brief-normalizer` | `superpowers` | Broader reasoning loop. Use `task-brief-normalizer` for rough prompt cleanup. |
| `ponytail-lite` vs `omo:refactor` | `omo:refactor` for code changes, `ponytail-lite` for scope control | One changes code structure; one constrains approach. |
| `omo:lsp` vs `code-navigation` | `code-navigation` for explanation, `omo:lsp` for diagnostics or rename checks | Use the narrower need. |
| `omo:visual-qa` vs `playwright` | `visual-qa` for appearance, `playwright` for behavior | They verify different surfaces. |
| `browser:control-in-app-browser` vs `chrome:control-chrome` | `browser` for current Codex browser, `chrome` for logged-in Chrome | Choose target browser state. |
| `github:github` vs `github:yeet` | `github` for reading/context, `yeet` for publishing | Do not publish unless the user asked. |
| `docs-sync` vs `writing-polish` | `docs-sync` for factual sync, `writing-polish` for wording | Use both when docs must be accurate and polished. |
| `codex-security:security-scan` vs `security-best-practices` | `security-scan` | Full scan is primary; best practices are lightweight advice. |

## Stop Rules

Do not add more skills just because they are related.

Use more than three total skills only when:

- the task explicitly spans multiple surfaces
- a verification skill is required after implementation
- a security or privacy review is part of the requested done condition

When uncertain, choose the primary skill and state the omitted candidate as a fallback.
