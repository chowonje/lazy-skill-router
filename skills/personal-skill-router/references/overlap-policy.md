# Overlap Policy

Primary skills own the requested action. Supporting skills add bounded context or constraints. Verification skills check
the result after the work.

## Tie Breakers

Apply these rules only to candidates present in the current app catalog:

1. Exact product, format, or workflow support beats a generic domain match.
2. An executor beats a planner or style guardrail for implementation work.
3. A state-aware integration beats generic automation when the request depends on an existing signed-in session.
4. A repeatable automation tool beats ad hoc interaction when the user asks for reproducible verification.
5. A focused security, privacy, or migration workflow beats generic review for that exact risk.
6. A verifier remains verification unless the request itself is solely a readiness check.

If the intended action still does not distinguish the candidates, do not inject either automatically. Record a narrow
shadow route only after synthetic positive and negative examples separate the intent.

## Stop Rules

Do not add more skills just because they are related.

Use more than three total skills only when:

- the task explicitly spans multiple surfaces
- a verification skill is required after implementation
- a security or privacy review is part of the requested done condition

When uncertain, abstain from automatic routing. The main agent may still choose an available skill explicitly.
