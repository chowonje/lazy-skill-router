# Build Week judge demo

This tranche adds one reproducible fixture for showing three skill choices against the same small project. It does not
change the product runtime, install a hook, touch `~/.codex`, enable Top-K activation, or broaden release claims.

## Judge quick check: no install or rebuild

Requirements: Git and Python 3.9 or newer on macOS or Linux. From the repository root, run one command:

```bash
python3 scripts/judge_playground.py
```

Expected decisions:

- mind map: `project-mindmap`, `propose`, reason `answer_only`;
- minimal retry: `ponytail`, `activate`, reason `eligible`.
- unsupported task: no recommendation, `abstain`, reason `no_candidate`.

The same command verifies exactly six fixture tests, compiles the fixture, and processes the synthetic sample event in
a self-deleting temporary directory. It does not install the router, access the network, write to `~/.codex`, or change
repository files. The router check does not require the skills to be installed because it evaluates an explicit,
repository-owned demo policy. Executing a selected skill inside Codex still requires that skill to be available and
requires fresh agent authorization.

Add `--json` to emit `lazy-skill-router.judge-playground/v1`. The JSON reuses the existing `route-result/v2` decision
and `skill-recommendation/v1` authority semantics, while omitting raw prompt text, absolute paths, regular expressions,
and timing data. Add `--skip-fixture-verification` for a routing-only check. For one custom prompt, run
`python3 scripts/judge_playground.py --prompt-stdin` and paste the prompt into stdin so it is not placed in shell
history or the process list. A skipped check is reported as `skipped`, never as `passed`.

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

## Show another router decision

Run `python3 scripts/judge_playground.py --prompt-stdin`, paste one prompt, and send EOF to replace the three built-in
decisions. The playground still uses the explicit demo policy, does not register or modify a Codex hook, and does not
echo the custom prompt.

## Verification

```bash
python3 scripts/judge_playground.py
```

`PASS` proves only the fixture's baseline behavior. It is not a security approval. The example is intentionally outside
the wheel and source-distribution allowlists. Judges can run it directly from the repository, while product packaging
remains unchanged.
