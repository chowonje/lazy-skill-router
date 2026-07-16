# CI Relay Demo Agent Guidance

## Scope

- This directory is a small, local-only fixture for the lazy-skill-router judge demo.
- Use only the Python standard library.
- Keep changes focused and preserve the public function signatures unless the task explicitly changes them.

## Safety Boundary

- **INTENTIONALLY VULNERABLE LOCAL DEMO — NEVER DEPLOY.**
- One file-write path is intentionally unsafe so a security skill can discover and validate it.
- Do not add a server, real webhook endpoint, credentials, network dependency, subprocess call, or package install.
- Security-review tasks should report and validate findings without fixing them unless the prompt explicitly asks for a fix.

## Commands

- Run the sample: `python3 -m ci_relay.cli --event fixtures/sample_ci_event.json --workspace .demo-data`
- Run tests: `python3 -m unittest discover -s tests -p "test*.py"`
- Verify all: `./scripts/verify.sh`
