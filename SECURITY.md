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
- Generated manifests use relative locator references, content digests, and bounded `name`/`description` frontmatter
  metadata rather than absolute local paths or full skill bodies.
- Host catalog drafts contain only skill names, descriptions, provenance class, enabled state, and implicit-invocation
  state. Skill descriptions are untrusted metadata and are never executed as instructions.
- Policy proposals must use synthetic examples rather than copied private prompts. The app LLM emits structured JSON,
  not Python, shell commands, hook registration, or executable code.
- Policy proposal v2 accepts canonical bindings and identifier-safe route, intent, pattern, and configured skill names,
  but no free-form route reason or pattern label. The hook emits only validated pattern IDs and a fixed router-owned
  reason. Proposal v1 remains compatible, but its identifiers pass the same restrictions and its reason and labels are
  discarded before model-visible context is built.
- Policy feedback stores route and proposal identifiers, verdict/source metadata, and previously hashed session/turn
  linkage. It does not add raw prompts or responses to the journal.
- Do not place secrets, tokens, credentials, `.env` values, or private data in route labels, reasons, or skill metadata.

## Config And Filesystem Trust

Explicit, environment, installed-personal, and bundled config discovery have separate advisory provenance. A config file
cannot elevate itself by setting the internal trust field. Invalid authoritative config fails open with no
recommendation and never falls through to a lower-precedence policy.

Any Policy IR parser error suppresses all runtime recommendations from that config. When a local inventory manifest is
configured, runtime scoring first verifies configured names, availability, uniqueness, and requested canonical IDs.
Missing, invalid, or canonical-mismatched inventory state cannot redirect a route to a same-name replacement provider.

Install validates and smoke-tests staged runtime before target mutation. Managed files are recorded in an ownership
manifest. Mutation snapshots use a path-confined local journal, and the next install recovers a matching interrupted
transaction before reading current state. Install, recovery, doctor, and uninstall reject artifact paths with symlinked
parents below the selected Codex home. Doctor reports managed artifact drift and skips executable smoke checks after
that drift is detected. Uninstall refuses a symlinked `hooks.json` write target. `uninstall --remove-files` does not
follow or remove leaf symlink targets and preserves modified or user-owned artifacts.

Skill inventory scanning refuses a symlinked `SKILL.md`, any symlinked parent below the selected skill root, and any
metadata file that resolves outside that root. Rejected entries are never read and are reported only with a
root-relative locator and reason code. An unchanged manifest-owned bundled skill may be upgraded automatically; a
modified, preserved, symlinked, unsafe, or unowned copy requires explicit `--force` replacement.

Release distributions are built once. The same verified artifact bundle and checksum manifest are passed to PyPI and
the GitHub Release job under separate least-privilege permissions.

Policy compilation never writes the active route file. Staging verifies the exact base-config revision and that all
existing routes remain unchanged before adding shadow candidates. Promotion accepts only feedback linked to observed
shadow decisions from the same config revision that would win after activation, requires current inventory and host
catalog revisions, requires explicit approval, and creates a backup before activating a route. App-generated patterns
use a restricted regex subset without general unbounded quantifiers, lookaround, or backreferences.

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
