# Support

## Support Levels

| Environment | Level | Current evidence |
| --- | --- | --- |
| macOS with POSIX filesystem semantics, Python 3.9+ | supported-with-constraints | source and Python 3.9 tests, package/install/doctor/uninstall smoke, isolated Codex CLI 0.144.0 shadow-hook canary, and v0.3/v0.4 rollback canary |
| Ubuntu 22.04 x64 / Python 3.9 and Ubuntu 24.04 x64 / Python 3.11 or 3.14 | supported-with-constraints | PR #3 hosted CI passed the full source, package, install, measurement, doctor, and uninstall workflow on all three combinations |
| Other Linux distributions or architectures | experimental | no equivalent hosted source-and-package matrix yet |
| WSL | unverified; out of scope for 0.4.0 support | no current package, path, symlink, or hook registration matrix |
| Native Windows | unsupported | standalone command, quoting, filesystem, and hook behavior are not implemented or verified for Windows |

`supported-with-constraints` means the documented default v1 behavior and opt-in unreleased contracts are covered by
the current source evidence. It does not mean every Codex build, third-party skill, plugin, connector, or custom hook
combination is supported.

WSL is not a 0.4.0 release blocker because this release does not claim WSL support. Treat it as unsupported in practice
until a dedicated package, path, symlink, hook-registration, and rollback matrix passes.

## Supported Surface

- Python `>=3.9`
- source checkout and wheel-installed `lazy-skill-router` CLI
- standalone copied `UserPromptSubmit` hook using canonical `python3` argv
- conditional copied `Stop` hook for measurement-enabled installations
- route config v1 default behavior
- opt-in schema v2 and versioned JSON diagnostics in the unreleased working tree
- generated inventory and install ownership manifests
- read-only doctor checks and ownership-aware uninstall
- local measurement event journal plus `outcome` and `report` CLI commands

## Before Reporting A Problem

Run these commands without including private prompt text or credentials in the report:

```bash
lazy-skill-router --version
lazy-skill-router doctor
lazy-skill-router route --json "sanitized reproduction prompt"
```

Include the operating system, Python version, installation method, exit code, and sanitized error text. Do not attach
`.env` files, tokens, auth stores, raw private prompts, private paths, or complete user configuration unless explicitly
needed and redacted.

## Compatibility

The released v0.3 contract is recorded in [`CURRENT_PUBLIC_CONTRACT.md`](CURRENT_PUBLIC_CONTRACT.md). The current source
targets `0.4.0`; its release notes are in [`CHANGELOG.md`](CHANGELOG.md). Unreleased strategy implementation and
remaining gates are recorded in
[`UNRELEASED_STRATEGY_IMPLEMENTATION.md`](UNRELEASED_STRATEGY_IMPLEMENTATION.md).

Structured recommendation, Hook IR, schema v2 defaulting, and broader platform support must
be announced through a versioned release and migration note before being treated as stable defaults.
