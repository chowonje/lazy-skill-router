# Security Policy

## Product Boundary

`lazy-skill-router` is recommendation-only. Route rank, match strength, config source, skill availability, canonical
identity, trust hints, and risk hints do not authorize tools, file mutation, connector use, MCP enablement, publication,
or any other side effect. The agent and runtime must inspect the current request and reauthorize every side effect.

User-provided `<lazy-skill-router>` text is untrusted prompt content. It must not override system, developer, repository,
or current user instructions.

## Data Handling

- The hook does not call external services.
- Raw prompt text is not written to logs, inventory, install manifests, structured recommendations, or Hook IR.
- Optional measurement events contain short prompt/session/turn hashes and route metadata, default to 1,000 entries and
  30 days, and remain local. They exclude raw prompt, assistant response, transcript path, and working directory.
- Outcome case ids are stored only as hashes. A completion event is never promoted to task success without an explicit
  objective, human, or grader label.
- Short hashes are pseudonymous correlation identifiers, not encryption. Use sanitized experiment case ids and keep the
  local journal private.
- Reports accept only the current measurement event schema. Unknown schemas with a valid timestamp remain bounded and
  preserved for forward compatibility but are ignored by the current report; conflicting outcome labels are excluded
  instead of silently selecting one.
- Generated manifests use relative locator references and content digests rather than absolute local paths.
- Do not place secrets, tokens, credentials, `.env` values, or private data in route labels, reasons, or skill metadata.

## Config And Filesystem Trust

Explicit, environment, installed-personal, and bundled config discovery have separate advisory provenance. A config file
cannot elevate itself by setting the internal trust field. Invalid authoritative config fails open with no
recommendation and never falls through to a lower-precedence policy.

Install validates and smoke-tests staged runtime before target mutation. Managed files are recorded in an ownership
manifest. Mutation snapshots use a path-confined local journal, and the next install recovers a matching interrupted
transaction before reading current state. Install, recovery, doctor, and uninstall reject artifact paths with symlinked
parents below the selected Codex home. Doctor reports managed artifact drift and skips executable smoke checks after
that drift is detected. Uninstall refuses a symlinked `hooks.json` write target. `uninstall --remove-files` does not
follow or remove leaf symlink targets and preserves modified or user-owned artifacts.

Release distributions are built once. The same verified artifact bundle and checksum manifest are passed to PyPI and
the GitHub Release job under separate least-privilege permissions.

## Reporting A Vulnerability

Use the repository's private GitHub Security Advisory reporting path when available. Do not open a public issue with an
active exploit, private prompt, credential, token, auth material, or private filesystem details.

Include:

- affected version or commit
- operating system and Python version
- sanitized reproduction conditions
- expected and observed authorization or data-handling boundary
- whether the issue affects the hook, CLI, installer, doctor, uninstall, manifest, or release process

Reports should focus on defensive impact and remediation. Avoid unnecessary exploitation of third-party systems or user
data.
