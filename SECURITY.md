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
- Legacy `route --json` and source `--dry-run` are operator-only diagnostics and may expose local regexes and deferred
  skill names. Do not forward them to an LLM or treat them as a sanitized wire contract.
- Optional measurement events contain short prompt/session/turn hashes and route metadata, default to 1,000 entries and
  30 days, and remain local. They exclude raw prompt, assistant response, transcript path, and working directory.
- Outcome case ids are stored only as hashes. A completion event is never promoted to task success without an explicit
  objective, human, or grader label.
- Short hashes are pseudonymous correlation identifiers, not encryption. Use sanitized experiment case ids and keep the
  local journal private.
- Reports accept only the current measurement event schema. Unknown schemas with a valid timestamp remain bounded and
  preserved for forward compatibility but are ignored by the current report; conflicting outcome labels are excluded
  instead of silently selecting one.
- Generated manifests use relative locator references, streaming content digests, and bounded `name`/`description`
  frontmatter metadata rather than absolute local paths or full skill bodies. Frontmatter parsing is limited to the
  first 64 KiB and 200 lines; skill documents over 1 MiB are marked `skill_document_too_large` instead of being hashed.
- Host catalog drafts contain only skill names, descriptions, provenance class, enabled state, and implicit-invocation
  state. Skill descriptions are untrusted metadata and are never executed as instructions.
- Policy proposals must use synthetic examples rather than copied private prompts. The app LLM emits structured JSON,
  not Python, shell commands, hook registration, or executable code.
- Policy proposal v2 accepts canonical bindings and identifier-safe route, intent, pattern, and configured skill names,
  plus identifier-safe activation facets, but no free-form route reason or pattern label. The hook emits only validated
  pattern IDs and a fixed router-owned reason code. Proposal v1 remains compatible, but its identifiers pass the same
  restrictions and its reason and labels are discarded before model-visible context is built.
- Runtime `propose` is candidate-only: it activates no skill. Only strong, non-ambiguous `activate` decisions select the
  primary skill; supporting and verification roles remain deferred. Router-meta matches hard-abstain without emitting
  model-visible context, and versioned structured contracts suppress all skill lists for the same abstention.
- Policy feedback stores route and proposal identifiers, verdict/source metadata, and previously hashed session/turn
  linkage. It does not add raw prompts or responses to the journal.
- Do not place secrets, tokens, credentials, `.env` values, or private data in route labels, reasons, or skill metadata.

## Config And Filesystem Trust

Explicit, environment, installed-personal, and bundled config discovery have separate advisory provenance. A config file
cannot elevate itself by setting the internal trust field. Invalid authoritative config fails open with no
recommendation and never falls through to a lower-precedence policy.

Any Policy IR parser error suppresses all runtime recommendations from that config. When a local inventory manifest is
configured, runtime scoring first verifies configured names, availability, uniqueness, and requested canonical IDs.
Missing, invalid, canonical-mismatched, or cross-name duplicate canonical inventory state cannot redirect a route to a
replacement provider. An unresolved default verification skill is removed before runtime route projection.

Install validates and smoke-tests staged runtime before target mutation. Managed files are recorded in an ownership
manifest. Mutation snapshots use a path-confined local journal, and a live install recovers a matching interrupted
transaction before reading current state; `--dry-run` validates and reports the pending recovery without restoring or
deleting anything. Install applies path confinement before reading `hooks.json` or the ownership manifest. Hook
replacement and removal require an exact canonical command or a command recorded by a valid, confined ownership
manifest; marker substrings do not establish ownership. Install, recovery, doctor, and uninstall reject artifact paths
with symlinked parents below the selected Codex home. Doctor reports managed artifact drift and skips executable smoke
checks after that drift is detected. Uninstall refuses a symlinked `hooks.json` write target or ownership-manifest
parent. `uninstall --remove-files` does not follow or remove leaf symlink targets and preserves modified or user-owned
artifacts.

Skill inventory scanning refuses a symlinked `SKILL.md`, any symlinked parent below the selected skill root, and any
metadata file that resolves outside that root. Rejected entries are never read and are reported only with a
root-relative locator and reason code. An unchanged manifest-owned bundled skill may be upgraded automatically; a
modified, preserved, symlinked, unsafe, or unowned copy requires explicit `--force` replacement. Human sync reports
escape Unicode control and formatting characters before writing them to a terminal.

Release distributions are built once. Checksum verification requires a non-empty manifest whose contained,
non-symlink paths exactly cover the selected artifact root. The same verified artifact bundle and checksum manifest are
passed to PyPI and the GitHub Release job under separate least-privilege permissions.

Policy compilation never writes the active route file. Staging verifies the exact base-config revision and that all
existing routes remain unchanged before adding shadow candidates. Promotion accepts only feedback linked to observed
shadow decisions from the same config revision that would win after activation, requires current inventory and host
catalog revisions, requires explicit approval, and creates a backup before activating a route. App-generated patterns
use a restricted regex subset without general unbounded quantifiers, lookaround, or backreferences.
Custom activation patterns use a conservative regex subset; only the exact audited bundled activation patterns are
allowlisted outside it.
Policy stage/promotion and host-catalog build reject leaf symlinks and every symlinked parent below the selected trusted
write root before backup or atomic replacement.

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
