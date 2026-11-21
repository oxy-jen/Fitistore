# FityStores email automation: readiness and migration gate

This is a local-code readiness record, not authorization to migrate, send mail,
enable a worker or activate marketing. It must be refreshed after isolated
PostgreSQL testing and before any production decision.

## Current implementation map

| Area | Current code evidence | Status / limit |
| --- | --- | --- |
| Provider delivery | `app/services/customer_mail.py` (`mail_configuration_status`, `_send_brevo`, `_send_resend`, `_send_smtp`) | Provider adapters and shared send path exist. This review does not prove live credentials, sender verification, DNS authentication, or inbox delivery. |
| Durable queue | `EmailMessage`, `EmailDeliveryAttempt`; `process_email_queue`; `deliver_email` | Queue, claim-token fencing, provider attempt metadata, bounded retry and ambiguous-outcome handling exist. Provider calls are outside the claim transaction. |
| Marketing eligibility | `app/services/marketing_eligibility.py`; `app/services/marketing_suppressions.py` | Shared eligibility/suppression policy is used at queue dispatch. PostgreSQL account-lock race semantics still need isolated PostgreSQL concurrency tests. |
| Investigation/recovery | `app/services/email_delivery_investigations.py`; admin email screen | Admin evidence and append-only event flow exists. Recovery queues a new, administrator-composed, escaped message only after confirmed provider non-acceptance. It never replays erased ambiguous content. |
| Worker liveness | `app/services/email_worker.py` | A unique run record and independent heartbeat are written during an explicit one-shot queue run. No evidence means no worker is reported healthy; this is not a permanent daemon. |
| Refund confirmation | `app/services/customer_mail.py` (`notify_refund_completed`), `app/admin/routes.py` (`update_refund`) | A transactional, idempotent outbox notice is queued only after an admin records refund completion with a reference. It uses the linked active, verified customer account and does not assert provider settlement timing. |
| Payment status failure notice | `app/admin/routes.py` (`update_order`), `app/services/customer_mail.py` (`notify_order_event`) | An idempotent transactional status notice is queued after an authenticated admin records a linked order's payment status as `Failed`. This reports the store's recorded status; it is not proof of an acquiring-provider failure event. |
| Requested-item restock notice | `app/services/inventory.py` (`queue_restock_alerts`), `app/services/customer_mail.py` (`notify_restock_alert`) | A narrowly scoped service notice is queued through the central outbox when stock becomes available for a pending email restock request with explicit contact consent. It is independent of marketing consent and performs no provider I/O. |
| Admin access | `app/admin/routes.py`, admin blueprint authorization and global CSRF handling | Investigation and recovery POSTs are admin-only and CSRF-protected. Provider configuration values are not editable through this feature. |
| Migration/guard | `app/services/email_migrations.py`, `app/services/email_outbox_schema.py`, `app/services/email_delivery_ops.py`, `app/services/email_legacy_reviews.py`, `app/services/email_test_purpose_migration.py`, `app/services/consent_audit.py`, `app/__init__.py`, `scripts/customer_email_schema_guard.py` | Explicit customer/email migration runs apply additive outbox purpose and consent-ledger protections in the existing transaction/advisory-lock boundary. PostgreSQL catalog behavior has not been exercised in this run. |
| Legacy payload review | `app/services/email_legacy_reviews.py`, `app/services/customer_mail.py`, admin email center | Metadata-only inspection identifies incomplete payloads. Worker dispatch fails closed until review schema is present; malformed queued rows are quarantined with their original fields preserved and an append-only event. Admin review requires an authenticated admin POST and CSRF token. |
| Isolated preflight | `scripts/email_migration_preflight.py` | Identity-pinned to the approved restore-test endpoint/database/role and PostgreSQL major; it sets the transaction READ ONLY and reports catalog/aggregate metadata only. Connection establishment could not be completed in this run, so no database result is claimed. |
| Marketing scheduling | `app/services/marketing_campaigns.py`, `app/services/customer_mail.py` (`process_marketing_email_queue`), `.github/workflows/process-marketing-email-queue.yml` | Admin-authored campaigns can be previewed, approved, scheduled, paused/cancelled and reported. The gated queue polls due approved campaigns every 15 minutes. Legacy daily/weekly/cart automation remains a no-op; automatic draft generation and per-cart idle scheduling are not active. |
| Templates | `app/templates/emails/`, `app/services/email_studio.py`, `app/admin/email_studio.py` | Shared branded templates and Studio exist. The inventory is summarized in `docs/email-system-inventory.md`; that document should be kept aligned with future route/template changes. |
| Studio purposes and template inventory | `app/services/email_purpose.py`, `app/services/email_template_inventory.py`, `app/templates/admin/studio_messages.html`, `docs/email-template-inventory.md` | Purpose selector now separates campaign, transactional/order service, and custom customer service. Code-backed library records implemented versus missing account/order/service/operational templates. Marketing scheduler remains paused. See detailed comparison and local test coverage in `docs/email-template-inventory.md`; no schema change is required. |

## Central settings audit

`MarketingAutomationSettings` in `app/models.py` has Python defaults of global
disabled, Daily Goodies disabled, Weekly Picks disabled, abandoned cart
disabled, and timezone `Africa/Nairobi`. A missing row does not materialize
these defaults in PostgreSQL; SQLAlchemy Python defaults apply only when a row
is inserted through SQLAlchemy. The recorded production inspection said the
table had zero rows, but this local task did not reconnect to production.

The current `run_marketing_automation()` is explicitly a no-op returning
`paused`; no settings read or automation dispatch occurs there. The settings
model has no matching admin CRUD route, so
there is not a complete admin settings interface for campaign hours, timezone,
cooldowns, or activation. Provider credentials and sender configuration are
environment-based in `config.py`; secret values must remain private. Keep
marketing paused until a separately reviewed settings UI, scheduler, eligibility
tests, and approval gate are complete.

## Additive migration inventory

Customer/email migration history includes the legacy account/email migrations,
`20261113_email_outbox`, `20261116_email_delivery_ops`,
`20261117_email_legacy_review`, `20261117_email_admin_test_purpose`, and
`20261117_customer_consent_audit_immutable`. The separate PostgreSQL suppression
migration is `20261113_marketing_suppression`.

`20261117_email_admin_test_purpose` corrects only outbox rows whose trusted kind
is `admin_test`, setting purpose to `test`; it does not alter recipients,
payloads, other message kinds, or delivery state. This is separate from the
historical outbox migration so its recorded checksum remains intact.
`20261117_customer_consent_audit_immutable` adds database triggers that reject
updates/deletes (and PostgreSQL truncation) on `customer_consent_event`; it
does not rewrite consent history. Both are additive and run only through the
explicit customer/email migration command. Neither migration has been executed
by this local implementation task.

`20261116_email_delivery_ops` adds `email_delivery_attempt`, `email_delivery_investigation`,
`email_delivery_investigation_event`, and `email_worker_run`, plus their indexes
and append-only investigation-event trigger(s). It does not drop/recreate
existing business tables or rewrite customer, order, product, or administrator
rows. It records a checksum in the existing `email_schema_migration` ledger.
The application migration helper creates only those four model tables in the
caller-owned transaction and validates the result before commit. The regular
explicit `migrate-customer-email-schema` command already calls it under the
existing PostgreSQL advisory transaction lock.

`20261117_email_legacy_review` adds the append-only
`email_legacy_payload_review_event` table and protects it with PostgreSQL
triggers. The migration does not edit existing outbox rows. Legacy historical
schema reconciliation is explicit and records `reconciled_schema`, not
`applied`, when original file execution cannot be proven.

Related customer/email and suppression migration commands use the same
nonblocking PostgreSQL transaction advisory lock. Lock contention fails
closed. The customer/email migration ledger detects unknown IDs and checksum
mismatches. Migration execution is **not** automatic at web startup. SQLite
transaction and trigger tests do not prove PostgreSQL DDL rollback, sequence
defaults, trigger operation masks, lock handling, or concurrent behavior.

The only authorized later execution order is: verify the isolated target and
backup first; inspect with the read-only preflight; if the existing customer
email schema has no central ledger, perform the explicit schema reconciliation
only after separately reviewing the strict schema guard; apply the
customer/email migration (legacy account/email schema, outbox, admin-test
purpose correction, delivery operations, legacy payload review, and consent
audit immutability in that order); validate it; then apply and validate the
separate marketing suppression migration. No production migration command is
approved by this document.

## Migration readiness matrix

| Gate | Evidence needed | Current state |
| --- | --- | --- |
| Local implementation and regression | Full Python test suite, schema-guard tests, focused worker/investigation tests, `git diff --check` | Final local suite: **355 passed**, **0 failed**, **0 skipped**; see verification record below. |
| Isolated database identity | Privately verify Neon project ID/name/region, branch ID/name, endpoint, database, role, schema and server version against the restore-test project | The user supplied prior successful endpoint and SQL identity verification. A fresh preflight connection attempt did not complete, so this run does not independently reconfirm it. |
| Read-only baseline | Isolated restored backup, catalog/row-count snapshot and existing migration ledger | Preflight could not establish a connection; baseline remains uncollected in this run. |
| PostgreSQL migration | Run the official explicit migration against the isolated restore only; verify rollback/rerun, constraints, FK actions, indexes/predicates, sequence defaults, triggers/functions, checksum | Not performed; required before calling the migration ready. |
| PostgreSQL concurrency | Independent connections test provider suppression/withdrawal vs dispatch, simultaneous claims, recovery uniqueness, heartbeat ownership and transaction rollback | Not performed; SQLite is not evidence for these guarantees. |
| Provider and sender | Verify privately in Brevo: sender identity, API access, transactional classification, SPF/DKIM/DMARC and webhook sample/authentication | Not verified from local code. |
| Render / GitHub configuration | Verify intended database, `manage:app` CLI use, production secret presence, queue gate unset/false, webhook configuration, logs and plan support | No external settings were read or changed. |
| Controlled launch | Separate explicit approvals for DB migration, app deployment, transactional worker, then marketing automation | Not authorized; all scheduling remains paused. |

## Next safe sequence

1. Resolve why the approved isolated Neon endpoint is unreachable from the
   current execution environment; never fall back to production.
2. Re-run the identity-pinned read-only preflight and schema guard, then compare
   its catalog and row-count manifest with the restored baseline.
3. Review missing/partial migration history and the legacy-message impact
   report before separately approving any isolated migration.
4. After that separate approval, run rollback/rerun and two-connection
   concurrency checks on the isolated target.
5. Review outstanding template/workflow inventory and verify Brevo/Render
   settings privately.
6. Obtain separate approval for production migration, deployment, queue
   processing, and marketing activation. Marketing remains last and paused.

## Verification record

Update this section only with commands actually run:

- `python -m pytest -q -p no:cacheprovider --basetemp=<isolated temp directory>`:
  **355 passed**, **0 failed**, **0 skipped**, **4,638 warnings** (primarily
  existing `datetime.utcnow()` and SQLAlchemy legacy-query deprecations).
- Focused refund completion and system-template preview tests: **2 passed**,
  **0 failed**; no real email was sent.
- Focused payment-status failure, consented restock service notice, refund,
  Email Studio inventory and template-inventory tests: **4 passed**, **0 failed**;
  these use the local SQLite test fixture and mocked/no provider delivery.
- Focused schema-guard, operations, suppression, and delivery tests:
  **107 passed**, **0 failed**. New migration-history and preflight tests:
  Migration history, isolated preflight, PostgreSQL CHECK normalization, and
  migration-wrapper safety tests: **12 passed**, **0 failed**.
- `python -m compileall -q app tests scripts`: **passed**.
- `git diff --check`: **passed** (Git emitted line-ending normalization notices
  for existing modified files; no whitespace errors).
- Python compilation: `python -m compileall -q app tests scripts` passed.
- Workflow YAML validation: **3 files passed** using `yaml.safe_load`.
- PostgreSQL preflight: **attempted read-only only**. The local DBAPI mismatch
  was corrected to select installed psycopg3. A retry produced no report and
  was interrupted after waiting; the connection/identity result is therefore
  unconfirmed. The tool issues only `SET TRANSACTION READ ONLY` and SELECT/catalog
  queries. No migrations or DML were issued.
- Recorded backup artifact: found at
  `C:\Users\Student\Downloads\fitystore_neon_20261113_080959.dump`; size
  202,955 bytes. SHA-256 matches the recorded digest
  `aed7ba20c65708c994550d19cdbad34b6ffdaa422f4f9415ce58f62ea33fbb2a`.
  The PostgreSQL custom archive signature is recognized, but `pg_restore` is
  unavailable here, so archive-list/readability validation remains outstanding.
- Production migration, real email delivery, worker activation, marketing
  activation, commit, push and deploy: **not performed**.
