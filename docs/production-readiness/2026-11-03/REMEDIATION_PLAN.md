# Sallie Returns remediation plan

Proposed only; no commands, installs, app runs, provider calls, or remote writes were executed. See [review](REVIEW.md).

## Dependency map

`SR-T1 → SR-T2 → SR-T3 → SR-T4 → SR-T5`

| Task | Depends on | Why |
| --- | --- | --- |
| `SR-T1` | none | establish an installable, accurately labeled baseline |
| `SR-T2` | `SR-T1` | browser/API trust boundaries should be set against the corrected product boundary |
| `SR-T3` | `SR-T1`, `SR-T2` | real-service wiring or an explicit demo-only split depends on known boundaries and server policy |
| `SR-T4` | `SR-T2`, `SR-T3` | meaningful tests need the intended auth/CORS/integration behavior fixed first |
| `SR-T5` | `SR-T4` | release/recovery acceptance depends on stable behavior and reproducible tests |

## Tasks

### SR-T1 — Restore a clean, accurately labeled baseline (`SR-1`, `SR-2`, `SR-5`)

Observed paths/ranges: `requirements.txt:1-3`; `README.md:97-107`; `returns_pipeline/version_a/tools.py:1-11,164-171,367-372,447-500,511-540,636-699`; `returns_pipeline/version_a/demo.py:1-11,133-180,200-369,384-413`.

1. Reproduce installation from a clean environment and confirm whether the literal `EOF` in `requirements.txt` blocks dependency resolution.
2. Remove the stray dependency line and choose a reproducible lock/install method for the actual Python entry points.
3. Trace every user-visible “real” outcome in README/demo copy back to the sampled helper functions and label each as `mock`, `manual`, or `authoritative integration required`.
4. Update README and demo-facing copy so “regression test cases” is only used for assertion-backed automation; otherwise rename the current coverage to demo scenarios.

Expected deliverable: installable baseline plus an integration-boundary matrix mapping each claimed effect to either mock code or a real dependency.

Verification procedure:

1. A fresh reviewer installs from the documented baseline without editing files ad hoc.
2. The reviewer compares the boundary matrix against `tools.py` and `demo.py` and confirms each row matches the source.
3. The reviewer checks README/demo text and confirms no simulated action is described as a completed live refund, live notification, or live supplier submission.

Definition of done:

- [ ] Clean-environment install/import succeeds without the stray `EOF` line.
- [ ] Every sampled provider-like action is labeled with its actual current behavior.
- [ ] Demo scenarios are no longer presented as independent automated regression evidence unless assertions and CI exist.

### SR-T2 — Define the browser/API trust boundary and cross-origin policy (`SR-3`, `SR-4`)

Prerequisite: `SR-T1`.

Observed paths/ranges: `returns_pipeline/version_a/app.py:5-10,68-88`; `returns_pipeline/version_a/tools.py:669-699`.

1. Inventory every preview route that can mutate case state, emit notifications, or simulate supervisor action, and document whether it is demo-only or intended for authenticated production use.
2. If the preview remains demo-only, explicitly isolate it as such and narrow CORS to the intended origin set; if it will be authenticated, add session/authn/authz and CSRF protections before keeping cross-origin POST enabled.
3. Add durable audit logging requirements for approve/reject/escalate actions, including actor identity, synthetic-vs-real mode, and whether any external side effect was only simulated.
4. Document a negative-test matrix for allowed origin, denied origin, preflight behavior, unauthenticated mutation, and forged supervisor-action attempts.

Expected deliverable: written trust-boundary spec and enforced header/auth policy for the preview surface.

Verification procedure:

1. Capture response headers for allowed and disallowed origins and confirm they match the documented policy.
2. Attempt unauthenticated or cross-role approval/rejection actions in a test environment and confirm denial is observable in logs and responses.
3. Confirm audit output records whether the action was simulated or produced a real external effect.

Definition of done:

- [ ] Cross-origin exposure is limited to the intended browser surface.
- [ ] Approval actions require the intended identity and role checks.
- [ ] Audit output distinguishes simulated actions from real side effects.

### SR-T3 — Either wire real integrations or keep a hard demo-only split (`SR-2`)

Prerequisite: `SR-T2`.

Observed paths/ranges: `returns_pipeline/version_a/tools.py:164-171,367-372,447-500,511-540,636-699`; `returns_pipeline/version_a/demo.py:397-413`.

1. For each mocked capability — receipt lookup, payment-token lookup, stock query/hold, transfer dispatch, supplier claim, and notification — decide whether the repo will integrate a real service or remain synthetic for that capability.
2. If integrating, define the authoritative adapter path, credential owner, synthetic test fixture, and fallback behavior for service failure.
3. If remaining demo-only, prevent UI/API copy from implying a completed external effect and keep manual/simulated branches explicit in logs and operator output.
4. Record evidence requirements for each integration: what output proves success, what output proves fallback, and what output proves no external action happened.

Expected deliverable: per-capability integration decision record and either real adapters or a locked demo-only boundary.

Verification procedure:

1. For any real adapter, observe a synthetic transaction against a non-production service and record the provider-side artifact ID or receipt.
2. For any demo-only capability, inspect UI/API output and logs to confirm the action is labeled simulated/manual.
3. Force each failure/fallback path and confirm the recorded output matches the documented behavior.

Definition of done:

- [ ] Each provider-like action has a named authoritative owner or an explicit demo-only restriction.
- [ ] No unresolved capability silently pretends to have completed a live external effect.
- [ ] Success, fallback, and no-op outputs are all defined and observable.

### SR-T4 — Turn the demo scenarios into assertion-backed automation (`SR-5`, preventive for `SR-3`/`SR-4`)

Prerequisite: `SR-T3`.

Observed paths/ranges: `README.md:97-107`; `returns_pipeline/version_a/demo.py:3-11,133-180,200-369,384-413`. Proposed paths: `tests/`, `.github/workflows/`.

1. Convert each named demo scenario into an assertion-backed test fixture with expected state transitions, expected terminal state, and explicit negative checks for the guardrail under test.
2. Add focused tests for wildcard/limited CORS behavior, denied approval attempts, duplicate hold attempts, supplier-claim fallback, and notification-mode labeling.
3. Run the suite from a clean environment in CI and fail the build on unmet assertions or dependency drift.
4. Keep the narrative demo runner only as a showcase; do not use it as the sole release gate.

Expected deliverable: reproducible automated coverage derived from, but not limited to, the current demo scenarios.

Verification procedure:

1. Deliberately weaken a role check or widen the CORS policy and confirm at least one negative test fails.
2. Deliberately break hold accounting or supplier fallback labeling and confirm the relevant test fails.
3. Re-run CI from a clean environment and confirm the same tests pass only when the intended behavior is restored.

Definition of done:

- [ ] Demo scenarios have assertion-backed counterparts.
- [ ] Negative tests fail when auth, CORS, or transaction invariants are broken.
- [ ] CI status is based on repeatable automated checks, not on a narrated demo alone.

### SR-T5 — Deployment, recovery, and final acceptance (`SR-2` through `SR-6`)

Prerequisite: `SR-T4`.

Observed paths/ranges: `index.html` and workflow HTML pages from the tree; `returns_pipeline/version_a/app.py:1-220`; `publication-evidence/sallie-returns-ai-agent.json`.

1. Separate static demo release mechanics from any authenticated backend release path and document which artifact is safe for public Pages hosting.
2. Define rollback, backup, and synthetic-data restore procedures for any backend state or audit records that matter to support or compliance.
3. Run a staging release with synthetic cases, observe rollback to a previous artifact, and observe restore of representative state.
4. Perform an independent acceptance pass that checks release identity, demo-vs-real labeling, auth/CORS policy, and the output evidence required by `SR-T3` and `SR-T4`.

Expected deliverable: observed staging release/recovery record and independent sign-off package.

Verification procedure:

1. Inspect the deployed artifact identity and confirm it matches the candidate commit.
2. Observe a restore of synthetic case data and verify the restored records are actually queryable.
3. Observe rollback to a previously verified artifact and verify the serving version changed back.

Definition of done:

- [ ] Static Pages and backend deployment scopes are explicitly separated.
- [ ] Restore and rollback are observed, not inferred from job success.
- [ ] Independent acceptance records unresolved limitations and does not over-claim live integrations.

## Production acceptance checklist

- [ ] Owner approves whether the product is demo-only, hybrid, or intended for live operations.
- [ ] Every external effect has either observed output evidence or an explicit demo-only label.
- [ ] Auth/CORS policy is intentionally configured and negative-tested.
- [ ] Automation is assertion-backed and CI-enforced.
- [ ] Release, rollback, and restore have been observed on synthetic data.

## Changes requiring owner approval

- Connecting to live POS, payment, WMS, supplier, courier, email, or SMS systems
- Storing or transmitting customer-identifying data outside the current mock fixtures
- Allowing browser-accessible supervisor actions on non-demo environments
- Choosing retention, audit, backup, or incident-response requirements

## Rollback and release policy

- Keep a clearly labeled demo artifact path separate from any live-service artifact path.
- Promote only immutable, candidate-identified artifacts.
- If any acceptance gate fails, roll back to the last independently verified artifact and preserve failure evidence for review.
