# Sallie Returns review

## Scope and verdict

Prototype implementation at `2eae62442129c71098fb6349f31d2842b837d9e9`. Sampled: `README.md:1-165`, `requirements.txt:1-3`, and `returns_pipeline/version_a/app.py:1-220` of 2,134 lines. The remaining application, orchestrator, tools, models and static pages were not inspected. No code, dependency installation, browser, payment or inventory operations were run. [Plan](REMEDIATION_PLAN.md).

## Architecture and boundaries

The README advertises a six-node returns state machine, stock holds and human review ([README.md:6-9](https://github.com/knarayanareddy/sallie-returns-ai-agent/blob/2eae62442129c71098fb6349f31d2842b837d9e9/README.md#L6-L9)); these claims are not proof of durable, integrated retail operations. `app.py` describes a Flask preview and imports an orchestrator and tools for receipt lookup, payment tokens, holds and supplier claims ([app.py:3-13,39-53](https://github.com/knarayanareddy/sallie-returns-ai-agent/blob/2eae62442129c71098fb6349f31d2842b837d9e9/returns_pipeline/version_a/app.py#L3-L53)). The tree separately lists `index.html` and workflow pages: the deployed static demo and Python backend must be evaluated as separate surfaces.

## Findings

- **SR-1 — High, confirmed static:** [`requirements.txt:1-3`](https://github.com/knarayanareddy/sallie-returns-ai-agent/blob/2eae62442129c71098fb6349f31d2842b837d9e9/requirements.txt#L1-L3) ends with literal `EOF`. It is not a normal versioned dependency declaration; installation is likely to fail unless such a distribution exists. Remove the stray line after reproducing the failure, then lock and audit resolved dependencies. Installation was not attempted.
- **SR-2 — High, unverified risk:** the preview says users can `approve / reject / escalate` ([app.py:5-10](https://github.com/knarayanareddy/sallie-returns-ai-agent/blob/2eae62442129c71098fb6349f31d2842b837d9e9/returns_pipeline/version_a/app.py#L5-L10)). The sampled prefix does not establish authentication, role authorization, CSRF or durable audit enforcement. Do not expose decisions to real users until these are traced and negatively tested.
- **SR-3 — High, unverified risk:** the advertised `atomic inventory holds (48h TTL)` ([README.md:9](https://github.com/knarayanareddy/sallie-returns-ai-agent/blob/2eae62442129c71098fb6349f31d2842b837d9e9/README.md#L9)) and imported tools do not establish a transactional inventory service. Confirm concurrency, expiry, idempotency and failure recovery against the actual integration before using real orders.
- **SR-4 — Medium, historical CI:** [Pages deployment](https://github.com/knarayanareddy/sallie-returns-ai-agent/actions/runs/41437031987) succeeded on the reviewed SHA. It establishes a recorded deployment outcome, not a backend test or security assessment.

## Five disciplines

- **Correctness:** orchestrator, state models and tools exist in the tree but were not read. Transitions, monetary rounding, retry semantics and real versus mock behavior need source and runtime verification.
- **Test automation:** the complete listed tree contains no test directory or workflow files; this does not prove there are no external tests. Establish independent state-machine and concurrent hold tests. The literal requirements defect blocks a clean baseline.
- **DevOps:** static Pages is historically deployed; backend hosting, configuration, observability, release and recovery are unknown. Separate demo and real-service release paths.
- **OWASP:** identity, authorization, CSRF, input injection, secrets, customer data retention and payment token handling are unverified; no secret values were examined. Dependencies have lower bounds but no observed lock in the tree. Use synthetic cases until authorization and data handling are proven.
- **UI/UX:** the preview and static interactive pages are user-facing, but their actual behavior, accessibility, error states and distinction between simulated and real actions were not tested. Approval actions need clear identity and consequences.

## Blockers and owner decisions

Owner must decide whether this remains a demo, which inventory/payment systems and roles are authoritative, who can approve exceptions, deployment platform, retention, credentials and recovery objectives. No real customer data or transactions should be used before these decisions and live-path checks. Source inventory is limited to the ranges above; no tests, security probes, accessibility checks, transaction integrations, backups or restores were executed.
