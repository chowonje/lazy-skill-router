# Promotion evidence protocol

## Purpose

`PromotionGateV1` may verify that declared evidence revisions match real local files. This is a content-identity check,
not proof that the files were independently produced or that their judgments are correct. The current verifier reports
`provesIndependence: false`, so the gate fails closed with `independent_evidence_unverified`. The gate still has
`authority: none`, never changes runtime policy, and cannot become `eligible-for-human-review` until a verifier
positively proves independence.

## Artifact binding

The experiment manifest may add all five optional locators under `evidence.artifactPaths`:

```json
{
  "artifactPaths": {
    "independentHoldout": "holdout.jsonl",
    "independentAdjudication": "adjudication.json",
    "ownershipObservation": "ownership.jsonl",
    "activationObservation": "activation.jsonl",
    "outcomeObservation": "outcomes.jsonl"
  }
}
```

Each locator is relative to an explicit local `--artifact-root`. Absolute paths, `..`, symlinked paths, reused paths,
non-regular files, empty files, and files larger than 16 MiB are rejected. On supported POSIX hosts the verifier walks
from an already-open root with `openat`/`O_NOFOLLOW`, keeps every artifact descriptor open, hashes that same descriptor,
stops the read loop at 16 MiB, and fails if its identity or metadata changes during the read. Platforms without these
primitives fail closed. The verifier compares actual bytes with the corresponding existing `*Revision` field. Paths,
file contents, and operating-system errors are never copied into the public report.

Omitting `--artifact-root` leaves verification `unavailable`. Supplying a root with missing or mismatched evidence
returns `evidence_artifact_verification_failed`.

```bash
python3 eval_router_ab.py eval/router_ab_manifest.json \
  --config ~/.codex/lazy-skill-router/routes.json \
  --inventory ~/.codex/lazy-skill-router/skills.manifest.json \
  --index ~/.codex/lazy-skill-router/capability-index.json \
  --artifact-root /path/to/blinded-evidence
```

Raw holdout prompts should remain outside the repository. Check in only prompt-redacted reports and reviewed lock
metadata when the experiment is ready for review.

## Independence boundary

The agent, subagents, and shared filesystem participants that saw the current 240-case corpus or bilingual pilot cannot
produce independent holdout cases, labels, adjudication, or metadata for this experiment.

Do not reuse the calibration prompts, case IDs, labels, translations, paraphrases, template expansions, or the
corpus-informed bilingual metadata. Reusing the same strata taxonomy and public skill descriptions is allowed.

The minimum human process is:

1. Freeze code, config, inventory, index, rubric, and experiment plan revisions.
2. Have an independent author create the holdout without access to calibration examples or results.
3. Preserve two blinded labels per case and resolve every disagreement before running A/B.
4. Record host ownership or semantic abstention, then link the resulting activation and outcome observations.
5. Freeze the five artifacts and place their SHA-256 revisions and relative locators in the manifest.
6. Run the full sample once. If results cause a code, metadata, threshold, or rubric change, retire that holdout to
   calibration and create a new holdout.

Current `RoutingObservationV1` ownership is deliberately `unobserved`, and current outcome events lack the complete
ownership/activation/rubric/runtime linkage. Neither source may be relabelled as promotion evidence without a new,
reviewed observation contract.

## Automated alternative boundary

사람 작성 holdout을 사용하지 않는 경우에는 prospective `AutomatedShadowEvidenceV1`만 수집한다. 이 artifact는
ranking 전에 exact configured-name reference를 결정론적으로 기록해 explicit-reference Recall@3/Top-1과 운영
latency를 측정한다. Raw prompt, prompt hash, session/turn hash, source path는 결과에 포함하지 않는다.

이 경로는 `PromotionGateV1`의 다섯 evidence artifact를 대체하지 않는다. Collection gate가 통과해도
`promotionStatus: blocked`, `authority: none`, `autoPromote: false`를 유지하며 semantic no-skill, ownership,
activation/outcome quality, independent authorship을 증명하지 않는다. 자세한 계약은
[`automated-shadow-evidence.md`](automated-shadow-evidence.md)를 따른다.

## Stop conditions

Stop and keep legacy behavior when any of these occurs:

- frozen revision mismatch, artifact verification failure, unverified independence, role-separation breach, or raw-prompt leak;
- active router changes during the experiment or a candidate changes after holdout results are viewed;
- Recall@3 below 95%, Top-1 below 90%, MRR below 75%, or mean Precision@3 below 30%; expected-abstain lexical
  no-match recall below 95%; paired Recall@3 uplift confidence lower bound at or below zero; B p95 above 20 ms;
- any forbidden Top-1 or high-risk forbidden Top-3 candidate;
- any inventory-ineligible candidate, operational failure, unresolved adjudication, invalid/conflicting outcome, or
  ownership-to-activation-to-outcome linkage gap;
- any high-risk safety harm, missed semantic abstention, or unplanned activation after abstention.

Passing file identity and metric checks is insufficient. Independently verified evidence is also required before human
review eligibility. Behavior promotion still requires a separate reviewed change, staged rollout, and explicit
rollback path.
