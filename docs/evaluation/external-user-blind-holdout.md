# External-user blind holdout

## Purpose and boundary

This source-only kit records a three-to-five-participant external-user usability study without storing prompts. It
measures only:

1. participant-reported recommendation appropriateness;
2. elapsed time from the recommendation becoming visible to the participant confirming the correct work start;
3. whether the participant understands that a recommendation does not authorize or execute work.

The result is descriptive usability evidence. It is not an independently adjudicated ranking benchmark, does not
prove that the router caused a faster start, and does not satisfy `PromotionGateV1`. Every report remains
`promotionStatus: blocked`, `authority: none`, `autoPromote: false`, and `retuningAllowed: false`.

Do not use the rows to tune code, metadata, patterns, thresholds, or the rubric. If any result informs such a change,
retire the study to calibration and collect a new blind study against newly frozen inputs.

## Collection order

Use `initialize_study()` once, then call `collect_case()` for each participant task. The collector deliberately has no
prompt argument. A caller may capture a prompt inside the injected router callback, but must not write it to the
JSONL.

`collect_case()` performs this order:

1. validate the frozen study header;
2. append the participant's expected skill token or `abstain` choice;
3. flush and `fsync` that expectation;
4. invoke the injected router callback, which may display the recommendation;
5. request the observation answers;
6. append the observation bound to the expectation's canonical revision.

If the router callback fails, the durable expectation remains as an incomplete case. A normal router failure should be
recorded as `runStatus: operational-failure`, `routerDisposition: unavailable`, and `fitVerdict: not-observable` so the
failure is retained rather than omitted.

The observation UI must use a monotonic clock and return `timeToCorrectStartMs` only when the participant confirms the
first correct work action. It returns `null` when the participant does not start. The fixed authority question is:

> Does this recommendation authorize or execute the skill by itself?

The correct choice is `recommendation-only`; the other choices are `authorizes-or-executes` and `unsure`.

## Promptless JSONL contract

All rows use `lazy-skill-router.external-user-holdout-row/v1`. The first and only `study` row freezes the existing A/B
input fields: config, inventory, index, index schema, retrieval algorithm, experiment code revision, and Top-K of three.
It also fixes the protocol revision, the exact three metric names, precommit requirement, no-retune rule, and
no-authority semantics.

Each case then has exactly two ordered rows:

- `expectation`: random participant and case IDs, `skill` or `abstain`, and an optional opaque skill token;
- `observation`: the expectation revision, router status/disposition, opaque recommended skill token, fit verdict,
  elapsed milliseconds, and authority answer.

IDs and skill tokens must be randomly assigned hexadecimal tokens. Do not derive participant IDs from a name, email,
account, device, or other low-entropy identifier. Skill tokens represent equality only; do not publish a mapping to
private skill names.

The exact-field schema has no prompt, prompt hash, timestamp, free-text note, identity, matched text, regular
expression, source path, or working-directory field. `rawPromptStored` must be `false` on every row. Unknown keys,
duplicate JSON keys, blank lines, invalid enums, non-finite or boolean durations, mismatched revisions, orphan rows,
duplicates, symlinks, unstable files, and artifacts above 16 MiB are rejected. Collection appends through a verified
parent descriptor and accepts an existing journal only when it is a single-link regular file with mode `0600`; it never
repairs an existing file's permissions.

Both successful and unsuccessful promptless rows are publishable after review. Do not publish a private prompt source
or any local token mapping with them.

## Validate and report

From the repository root:

```bash
python3 eval_external_user_holdout.py validate /path/to/promptless-holdout.jsonl
python3 eval_external_user_holdout.py report /path/to/promptless-holdout.jsonl
```

Exit `0` means the artifact contains complete cases from three to five unique participants. Exit `1` means the
structure is valid but collection is incomplete or outside that participant range. Exit `2` means schema, privacy, or
artifact validation failed.

The report contains audit counts plus exactly three metric objects. It includes source, plan, protocol, frozen-input,
and report revisions, but emits no participant, case, or skill tokens. Operational failures, incomplete cases, and the
three-to-five participant gate remain visible in audit counts. A report never populates the five independent
promotion-evidence artifacts.

Actual promotion evidence still requires independent authoring, two blinded labels with adjudication, complete
ownership-to-activation-to-outcome linkage, and a trusted independence verifier. Content hashes alone do not prove
independence or quality.
