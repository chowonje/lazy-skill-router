# Versioned Strategy Implementation

Status: released in `0.4.0` as opt-in functionality; not enabled as the default hook wire contract.

This document separates the opt-in strategy implementation from the v1-compatible default behavior in
[`CURRENT_PUBLIC_CONTRACT.md`](CURRENT_PUBLIC_CONTRACT.md). The default hook still emits the legacy top-1 advisory prose
block, and `route --json` still returns the existing v1 diagnostics. The filename is retained to preserve links from
pre-release reviews.

## Implemented Opt-In Contracts

The source hook and packaged CLI expose three deterministic JSON views:

```bash
lazy-skill-router route --route-result-v2 "GitHub PR에서 CI 실패 고쳐줘"
lazy-skill-router route --recommendation-json "GitHub PR에서 CI 실패 고쳐줘"
lazy-skill-router route --hook-ir-json "GitHub PR에서 CI 실패 고쳐줘"
```

- `lazy-skill-router.route-result/v2` returns up to three deterministically ranked routes, `match_strength`,
  `score_margin`, stable pattern evidence IDs, fallback state, and ambiguity state.
- `lazy-skill-router.skill-recommendation` version `1.0` adds recommendation-only safeguards, canonical skill
  references, inventory state, advisory config trust, and unresolved capabilities.
- `lazy-skill-router.hook-ir/v1` is a compact projection for shadow evaluation. It uses only conservative
  `explain`, `inspect`, and `verify` phase hints. It does not infer `mutate` or `publish`, grant permission, or request
  execution.

All three views exclude raw prompt text, matched prompt substrings, credentials, and absolute local paths. Existing v1
`confidence` remains a compatibility field. The versioned contracts call the same evidence score `match_strength` and
mark it as `not_probability`.

## Schema V2 Input

The runtime and validator accept an opt-in schema v2 policy with stable route IDs, intent, capability requirements,
stable pattern IDs, explicit skill bindings, weighted evidence, deterministic tie-breaking, and a post-selection
fallback route.

```json
{
  "schemaVersion": 2,
  "policyVersion": "2026-07-10.1",
  "selection": {
    "mode": "ranked",
    "maxRecommendations": 3,
    "minMatchStrength": 0.55,
    "minScoreMargin": 0.05
  },
  "skillBindings": {
    "pdf-work": "pdf",
    "change-verification": "verification-gate",
    "general-assistance": "personal-skill-router"
  },
  "fallbackRouteId": "general",
  "routes": [
    {
      "id": "pdf",
      "intent": "work_with_pdf",
      "capabilityRequirements": {
        "primary": ["pdf-work"],
        "verification": ["change-verification"]
      },
      "match": {
        "any": [
          {"id": "pdf.token", "regex": "pdf", "label": "PDF token", "weight": 2}
        ],
        "none": []
      }
    },
    {
      "id": "general",
      "intent": "general_assistance",
      "capabilityRequirements": {"primary": ["general-assistance"]}
    }
  ]
}
```

Unsupported schema versions fail open with no recommendation. A v2 route with no bound primary capability is skipped.
The default policy remains route config v1 until shadow fixtures and release gates justify migration.

## Skill Inventory

`sync_skills.py` can write a version-stamped, path-redacted inventory:

```bash
python3 sync_skills.py \
  --routes ~/.codex/lazy-skill-router/routes.json \
  --manifest-output ~/.codex/lazy-skill-router/skills.manifest.json
```

The installer generates the same manifest automatically. Entries include canonical provider identity, configured name,
bounded frontmatter description, relative locator reference, content digest, revision when observable, aliases, and an availability snapshot. Duplicate
configured names remain ambiguous. Runtime dependency, connector auth, MCP enablement, and managed allowlist states stay
`unknown` until a trusted runtime source can verify them; availability is never treated as authorization.

## Install Ownership And Recovery

Install now writes `install.manifest.json` with relative artifact paths, ownership mode, type, digest, expected hook
registration, and a revision over the canonical record. Doctor validates both the inventory revision and managed
artifact digests.

After staged hook smoke succeeds, target mutation runs inside a journaled rollback guard. If a later copy, manifest,
route, or `hooks.json` write raises, pre-existing files, directories, symlinks, and bytes are restored and transaction
backups are removed. If the process stops before cleanup, the next install for the same Codex home validates the
path-confined journal and restores the snapshots before reading or mutating install state. Hook registration remains the
final mutation.

`uninstall --remove-files` removes only artifacts covered by a valid ownership manifest whose current digest still
matches. Modified files, symlinks, and preserved user artifacts remain in place. Default uninstall still removes only
the hook registration.

Journal paths are relative to the selected Codex home, backup paths are confined to the transaction directory, and a
Codex-home fingerprint prevents a journal for one home from being replayed against another. Invalid or escaping journal
paths fail the install rather than being applied. Artifact path `.` is invalid, and symlinked parents below the selected
Codex home are unsafe so install, recovery, doctor, and uninstall do not traverse into an external target.

## Logging And Trust

Measurement remains opt-in. `activation.mode` supports `inject`, `shadow`, and `off`; missing configuration preserves the
legacy `inject` behavior. `install --enable-measurement --activation-mode shadow` registers a conditional `Stop` hook and
accumulates versioned decision and completion events. Disabling measurement removes the Stop registration.

Events store only hashed prompt/session/turn identifiers and route/version metadata. They exclude raw prompts, assistant
responses, transcript paths, and working directories. Writes are lock-protected, atomic, and bounded to 1,000 entries and
30 days by default. `logging.maxEntries` and `logging.retentionDays` remain configurable within runtime caps.

`lazy-skill-router outcome` appends an explicit objective, human, or grader label using a hashed case id and records the
policy/config revision when `--config` is supplied. `lazy-skill-router report` correlates completion with session and turn
hashes together, deduplicates same-status labels, excludes conflicting labels, prevents native/inject pairs across
revision contexts, and reports mixed, unversioned, invalid, or ignored evidence. Completion alone is never treated as
success.

Config discovery and config trust are separate. The loader derives advisory trust from explicit, environment,
installed-personal, or bundled discovery and overwrites any `_config_trust` value claimed inside the JSON file. The
structured contracts explicitly state that config trust and availability do not authorize work.

## Remaining Default-Activation Gates

- Keep structured recommendation and Hook IR opt-in while evaluation contracts mature; an isolated macOS Codex CLI
  0.144.0 shadow-hook canary now verifies actual `UserPromptSubmit` and `Stop` delivery.
- Add trusted runtime probes before any inventory entry can become fully `eligible`.
- Add an explicit phase policy before emitting `mutate` or `publish`; runtime permission remains authoritative.
- Keep the optional low-margin LLM branch disabled until privacy, latency, labeled quality, and fail-open gates exist.
- Ubuntu 22.04 x64 / Python 3.9 and Ubuntu 24.04 x64 / Python 3.11 and 3.14 now pass the full hosted workflow. Other
  Linux distributions and architectures remain experimental. WSL is unverified and native Windows is unsupported;
  neither blocks 0.4.0 because this release does not claim support for them. The local Python 3.9 v0.3/v0.4 upgrade and
  rollback matrix passes when measurement is disabled before downgrade.
- Version 0.4.0 was tagged at `f42c8384709893548dfd5bd8a0ef828627460046` and published to PyPI and GitHub Releases
  through release run `29096430451`.

The 0.4.0 publication gate is complete. Experiment manifests, objective evaluator automation, and trusted runtime
eligibility remain future default-activation gates rather than retroactive blockers for the opt-in release.
