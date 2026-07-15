# Devpost submission draft

## Project name

Lazy Skill Router

## Tagline

A local recommendation layer that helps Codex choose the right installed skill without turning recommendations into
permission.

## Track

Developer Tools

## Project description

Lazy Skill Router is a local, deterministic skill recommendation layer for Codex. It reads the current prompt and
returns an `activate`, `propose`, or `abstain` decision while keeping recommendation separate from authorization.

Before Build Week, I used Codex with GPT-5.6 to evolve the project into Architecture 3. In that design,
deterministic routing and ActivationIR remain authoritative while capability-based Top-K retrieval runs only as a
guarded, non-activating shadow lane.

During Build Week, I used Codex with GPT-5.6 to meaningfully extend that foundation. I added a shared 4,096-character
input gate, conservative PolicyIR and regex validation, managed-root file protections, transactional install and sync
recovery, measurement-pure diagnostics, capability-index v2 compatibility, regression coverage, and a reproducible
judge demo. These changes make the tool safer to inspect and test without enabling automatic Top-K activation or
lowering promotion thresholds.

## What it does

- Discovers and describes locally available Codex skills without exposing their filesystem paths.
- Matches a prompt against a deterministic policy and produces a versioned routing decision.
- Activates one primary skill only when the evidence is strong and unambiguous.
- Defers weak, answer-only, ambiguous, and fallback matches for agent review.
- Ranks up to three capability candidates in a separate preview lane that cannot affect activation.
- Installs, synchronizes, diagnoses, and removes its managed Codex hook files with explicit safety boundaries.

## How I used Codex and GPT-5.6

GPT-5.6 helped me review the system across routing, policy parsing, installation, synchronization, diagnostics, and
evaluation instead of treating each file in isolation. It identified failure modes such as pathological regex input,
non-finite policy values, manifest/index drift, unsafe write boundaries, and diagnostic paths that could contaminate
measurement.

Codex accelerated the implementation loop by turning each reproduced defect into a characterization test, applying
focused patches, running regression checks, reviewing cross-file contracts, and keeping the README and evidence
boundary synchronized with the code.

I retained the product decisions: deterministic routing remains authoritative, Top-K stays in shadow, a recommendation
never grants permission, the original dirty source tree stays preserved, and passing an evaluation qualifies evidence
only for human review rather than automatic promotion or release.

## Build Week disclosure

- Prior foundation: Architecture 3, commit `6123ffe3acdc7ae7b35082ab8938d77fc8070872`, Codex session
  `019f522b-8db5-7211-84d4-889d8c9d9de8`.
- Submission-period extension: commits `561732d17c6ad479ba07b2b9cab73dcb05333f90` and
  `a62473470b120703f929a4026948550db1384627`, Codex session
  `019f6362-9c9b-76e2-b3d1-cffb12ebfc9d`.

Architecture 3 is disclosed as pre-existing work. The judged Build Week contribution is the separate stabilization and
reproducible-demo tranche described above.

## How judges can test it

The repository README contains a no-install quick start. Judges can clone the repository, run the router directly with
Python and the explicit demo policy, and verify the synthetic CI Relay fixture without building a package or modifying
their Codex home.

Supported judge path: macOS or Linux with Git and Python 3.9 or newer. Native Windows is not supported.

## Built with

- Codex
- GPT-5.6
- Python standard library
- GitHub Actions
