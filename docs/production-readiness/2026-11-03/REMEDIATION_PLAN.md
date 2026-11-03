# Sallie Returns remediation plan

Proposed only; no commands or checks were executed. See [review](REVIEW.md).

## Dependencies and gates

`SR-T1 → SR-T2 → SR-T3 → SR-T4 → SR-T5`. Baseline first, then transaction and security controls, then automation and independent release/UX acceptance. A static demo must not be promoted as an integrated service.

### SR-T1 — Restore a clean baseline (SR-1)
Prerequisite: owner approves isolated environment. Observed paths: `requirements.txt`, `returns_pipeline/version_a/app.py`.
1. Reproduce dependency resolution from a clean environment without real credentials.
2. Remove the unintended literal line and select an approved reproducible dependency-lock approach.
3. Record actual entry points, existing dependencies and mock versus external tools by tracing `orchestrator.py` and `tools.py`.
Deliverable: reproducible setup and integration map. Verification: independent clean-environment installation and import, with resulting versions recorded.
- [ ] Dependency installation succeeds without the stray declaration.
- [ ] Mock and real operations are explicitly distinguished.

### SR-T2 — Protect and reconcile decisions (SR-2, SR-3)
Prerequisite: SR-T1; owner approves role and transaction policies. Observed paths: `returns_pipeline/version_a/orchestrator.py`, `tools.py`, `state_models.py`, `app.py`.
1. Map each case transition, approval, inventory mutation and payment-token access to an authenticated actor and authoritative service.
2. Enforce authorization, request validation, CSRF protection where applicable, idempotency and transactional hold/expiry semantics at the actual server boundaries.
3. Implement a durable, redacted audit trail and define recovery for partial supplier, transfer and notification failures.
Deliverable: documented and enforced trust and transaction boundaries. Verification: concurrent requests, duplicate retries, unauthorized approval and expiry scenarios against isolated test services.
- [ ] Cross-role and unauthenticated actions are denied.
- [ ] Duplicate and competing holds cannot silently double-allocate.

### SR-T3 — Add meaningful regression automation (SR-1–SR-3)
Prerequisite: SR-T2. Proposed paths: `tests/`, `.github/workflows/`.
1. Create independently specified cases for each allowed and denied state transition, monetary edge and failure path.
2. Add integration tests for simultaneous holds, retry idempotency, audit redaction and authorization using synthetic data.
3. Define and run a reproducible test entry point in CI; record dependency-audit results without suppressing failures.
Deliverable: required, repeatable regression suite. Verification: deliberately break a role check and hold invariant separately and observe their negative tests fail.
- [ ] Tests detect both authorization and concurrency regressions.
- [ ] CI runs from a clean environment and preserves failure status.

### SR-T4 — Deployment and recovery (preventive)
Prerequisites: SR-T2 and SR-T3; owner chooses hosting, secrets, retention and recovery objectives. Observed paths: `index.html`, `returns_pipeline/version_a/app.py`; proposed paths: deployment and backup runbooks.
1. Separate static demo from any authenticated backend and disable real transactions in demo mode.
2. Configure least-privilege credentials, redacted logs, immutable releases and an approved data backup policy.
3. Perform a staging deployment, restore representative synthetic case data and roll back to a previous verified artifact.
Deliverable: observed release and recovery record. Verification: independently inspect restored state and artifact identity; do not infer recovery from a successful job alone.
- [ ] Secrets and customer-data lifetimes have approved owners.
- [ ] Staging restore and rollback are observed.

### SR-T5 — Usability and independent acceptance (preventive)
Prerequisite: SR-T4. Observed paths: `index.html`, `version_a_workflow.html`, `version_b_workflow.html`, `returns_pipeline/version_a/app.py`.
1. Label simulated versus real outcomes and show approval consequences and failure states.
2. Test keyboard, screen-reader and accessible form/error behavior across supported surfaces.
3. Independently rerun security, transaction and recovery checks against the candidate revision.
Deliverable: acceptance record with unresolved limitations. Verification: observe a complete synthetic return and an unauthorized approval attempt.
- [ ] Users can identify demo actions and recover from errors.
- [ ] Independent evidence and owner sign-off are recorded.

## Production gate and approvals

- [ ] Owner approves identity, transaction authority, integrations, data retention, deployment and recovery objectives.
- [ ] Negative tests, actual staging restore and candidate-specific acceptance all pass; exceptions are explicit.
- [ ] Roll back using a previously verified immutable artifact if a release gate fails.
