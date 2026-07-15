# CI Relay Demo

CI Relay is a deliberately small Python project used to demonstrate how `lazy-skill-router` selects different skills
for the same repository. It accepts a CI event, validates it, stores the run and its artifact, then emits a local
notification through an injected sender.

The project has no third-party dependencies and makes no network calls.

## Run it

```bash
python3 -m ci_relay.cli \
  --event fixtures/sample_ci_event.json \
  --workspace .demo-data
```

## Test it

```bash
./scripts/verify.sh
```

## Data flow

```text
event JSON -> webhook validation -> run record + artifact -> local notification -> JSON result
```

## Demo safety notice

**INTENTIONALLY VULNERABLE LOCAL DEMO — NEVER DEPLOY.** One bounded file-write flaw is retained for the security
scene. Run the fixture only in a disposable local directory. The normal test suite passes because it exercises the
intended application behavior, not the security probe.
