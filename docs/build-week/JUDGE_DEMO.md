# Build Week judge demo

This tranche adds one reproducible fixture for showing three skill choices against the same small project. It does not
change the product runtime, install a hook, touch `~/.codex`, enable Top-K activation, or broaden release claims.

## Judge quick check: no install or rebuild

Requirements: Git and Python 3.9 or newer on macOS or Linux. From the repository root, run the router against the
explicit demo policy:

```bash
python3 lazy_skill_router.py \
  --config docs/build-week/routes.judge-demo.json \
  --route-result-v2 \
  "Map this repository as a project mind map. Show the data flow. Do not modify files."

python3 lazy_skill_router.py \
  --config docs/build-week/routes.judge-demo.json \
  --route-result-v2 \
  "Add one retry on TimeoutError. Make the smallest correct change and add no dependency."

examples/ci-relay-demo/scripts/verify.sh
```

Expected decisions:

- mind map: `project-mindmap`, `propose`, reason `answer_only`;
- minimal retry: `ponytail`, `activate`, reason `eligible`.

The router check does not require the skills to be installed because it evaluates an explicit, repository-owned demo
policy. Executing the selected skill inside Codex still requires that skill to be available in the judge's Codex app.
The fixture verification uses only the Python standard library and the synthetic sample event in
[`examples/ci-relay-demo/fixtures/sample_ci_event.json`](../../examples/ci-relay-demo/fixtures/sample_ci_event.json).

## Optional: prepare an isolated recording session

On macOS, from the repository root:

```bash
python3 scripts/prepare_judge_demo.py
open "$HOME/Desktop/Lazy Skill Router Demo"
```

On Linux, run the Python command and open the printed session path with your file manager or Codex project picker.

The command never replaces an existing session. It creates three independent copies under a new numbered session:

```text
Lazy Skill Router Demo/
├── START_HERE.md
├── CURRENT.txt
└── session-001/
    ├── 01-mindmap/
    ├── 02-ponytail/
    ├── 03-security/
    └── SESSION.json
```

Open each numbered scene as a separate Codex project. Changes in one scene cannot affect another scene or the
canonical fixture in `examples/ci-relay-demo`.

## Natural-language prompts

### Project Mindmap

> Map this repository as a project mind map. Show the main components and the data flow from event JSON to notification. Do not modify files.

### Ponytail

> Add one retry to the notifier when TimeoutError occurs. Make the smallest correct change, preserve the public API, add one focused test, and add no dependency, sleep, or abstraction.

### Codex Security

> Scan this CI relay for a concrete exploitable security vulnerability before release. Validate it with a local proof, but do not modify files.

The security scene is optional and intentionally vulnerable. It is local-only; never serve or deploy it. A recording
may use only the mind-map and Ponytail scenes when the security skill is unavailable.

## Show the third router decision

The demo policy is explicit and separate from `routes.default.json`:

```bash
python3 lazy_skill_router.py \
  --config docs/build-week/routes.judge-demo.json \
  --route-result-v2 \
  "Map this repository as a project mind map. Show the main components and data flow."
```

Replace the final prompt with the security prompt to show the corresponding primary skill. This CLI-only demo does not
register or modify a Codex hook.

## Verification

```bash
examples/ci-relay-demo/scripts/verify.sh
PYTHONPATH=scripts python3 scripts/test_prepare_judge_demo.py
python3 validate_routes.py docs/build-week/routes.judge-demo.json
```

The example is intentionally outside the wheel and source-distribution allowlists. Judges can run it directly from
the repository, while product packaging remains unchanged.
