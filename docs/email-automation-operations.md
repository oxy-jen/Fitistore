# Email automation operations and retention

This document describes local application controls. It does not authorize a
production migration, queue run, marketing schedule, provider change, or deploy.

## Secret configuration

`APP_ENV=production` or Render's `RENDER=true` makes application startup require
an explicit `SECRET_KEY` of at least 32 characters, with sufficient character
variety and without the known development fallback or a short repeated pattern.
Development keeps its local fallback. Tests use an explicit test-only key.

In Render, open the production web service's Environment settings and privately
set `SECRET_KEY` to a high-entropy value generated once with a trusted password
manager or secrets manager. Do not paste it into chat, a commit, logs, or this
repository. Apply the same secret to every production web instance and any
production CLI/worker process. Restart/redeploy after setting it. This code does
not inspect or rotate Render's value. Rotating it invalidates existing signed
Flask sessions and any tokens whose signing depends on Flask's secret; plan a
customer reauthentication window and review signed-link lifetimes first.

## Message payload retention policy

| Message state | Rendered HTML/text policy | Outcome and metadata |
| --- | --- | --- |
| Queued non-auth message | Retain until dispatch; never cleanup while queued | Retain |
| Queued authentication message | Retain only until its verification/code lifetime; cleanup marks it failed and clears its body | Retain expiration outcome |
| Sending / active claim | Never cleanup | Retain |
| Retryable provider/infrastructure failure | Retain while within bounded retry budget and next attempt is scheduled | Retain |
| Provider accepted / sent | Sender clears payload immediately; cleanup clears any legacy body after 30 days | Retain |
| Permanent failure or suppression | Clear body after 30 days; do not alter status | Retain |
| Ambiguous provider outcome | Clear body immediately; never requeue automatically | Retain ambiguous outcome and provider metadata |
| Suppression and consent audit events | Do not purge through email payload cleanup | Retain; any future deletion policy requires separate privacy/legal review |
| Email message metadata/recipient | This command does not delete or anonymize rows | Retain until a separately approved retention schedule and referential-impact review exist |

The 30-day terminal-body period is a maximum for legacy payloads; normal
successful and ambiguous paths erase bodies sooner. Authentication expiry uses
the existing token lifetimes: 10 minutes for one-time codes, 30 minutes for
password reset links, and 24 hours for account verification/preference links.
Retrying messages are protected until their retry window ends. Customer/order
history and suppression/audit rows are outside this cleanup operation.
Rendered bodies are not application-layer encrypted by this code. Provider-
side database encryption and backups must be verified with the database host;
their current settings were not inspected during this local-only task.

Run a bounded report first:

```powershell
python -m flask --app manage:app cleanup-email-payloads --retention-days 30 --limit 200
```

After reviewing the counts and separately authorizing local/production use,
execute one bounded batch:

```powershell
python -m flask --app manage:app cleanup-email-payloads --retention-days 30 --limit 200 --execute
```

Repeat deliberately until counts are zero. Each invocation emits a run ID and
counts to stdout and the application logger. Configure the hosting log service
to retain these operational events under the approved logging policy. Dry-run
does not mutate rows. The command never prints message content or recipient
addresses and never deletes message rows.

## Queue worker model

The worker remains a one-shot outbox processor. Web application startup never
dispatches mail. It processes at most 50 rows by default (maximum 500) and
returns between messages when the configured time limit is reached or SIGTERM
requests a graceful stop. A provider call already in flight is allowed to
finish; an external hard kill during that call remains an ambiguous outcome and
must not trigger automatic resend.

Inspect metadata without dispatch:

```powershell
python -m flask --app manage:app email-queue-health
```

Process one bounded local batch only after separate approval and verified test
provider setup. The command requires the exact confirmation phrase:

```powershell
python -m flask --app manage:app process-email-queue --limit 25 --max-seconds 240 --confirmation "RUN EMAIL QUEUE"
```

The command emits JSON counts and returns a nonzero CLI result if mail is not
configured or the operation errors. Monitor queued counts, oldest queued time,
failed rows, and ambiguous outcomes. Use the existing stale-claim recovery
command only after reviewing provider activity; it marks rows ambiguous and
never resends them.

Each explicit queue batch writes one `email_worker_run` row with a unique
instance ID. While a long batch runs, an independent heartbeat thread updates
`last_heartbeat_at`; attempt and successful-dispatch timestamps are recorded
separately. The email admin dashboard shows the latest ten runs and marks a
STARTING/RUNNING/STOPPING run stale when its heartbeat exceeds the configured
`EMAIL_WORKER_HEARTBEAT_TIMEOUT_SECONDS` (90 seconds by default). A short
one-shot batch may show only its STARTING/RUNNING/STOPPED lifecycle because it
finishes before the periodic heartbeat interval. A live heartbeat is not proof
of provider acceptance. An empty queue is not proof that a worker is running.
An abrupt process kill leaves the last recorded active state, which becomes
STALE after the threshold; it is not falsely reported healthy.

If worker heartbeat storage cannot be written at startup, the batch fails
closed before dispatching any message. A later heartbeat-write failure is
logged by exception type only; the admin view will show the last stored
heartbeat and eventually report the worker as stale. Each run has its own
instance ID, so one worker's shutdown only closes that run.

### Email Studio scheduled campaigns

Email Studio can queue a reviewed marketing campaign for a future local time
using the configured `TIMEZONE` (Africa/Nairobi fallback). Scheduling requires
the exact recipient-count confirmation and a preview of the same content. The
accepted range is five minutes to 90 days ahead. Scheduling only persists the
message; it does not contact the provider. At dispatch, the worker rechecks
account activity, verification, consent, unsubscribe/suppression and frequency
limits. Frequency checks use the scheduled delivery time so campaigns planned
in advance cannot bypass the cooldown. Transactional messages cannot be
scheduled through Email Studio. Delivery still depends on the separately gated
queue workflow; marketing automation itself remains paused. A scheduled row is
not a provider acceptance or a guaranteed delivery.

### Email Studio test messages

Test messages are queued only to the server-configured `MAIL_TEST_ADDRESS`;
the submitted form cannot override it. The address must be syntactically valid
and must not match any customer account in the database. Configure it privately
to an address controlled by an administrator. A test still requires an exact
preview, the `SEND TEST` confirmation, configured sender settings, and the
per-admin rate limit. Test messages are labelled and queued as purpose `test`;
they are not transactional customer notices or marketing. Queue execution
remains subject to the separate explicit worker confirmation and workflow gate.

### Isolated transactional and administrator-test workers

The general `process-email-queue` command and its `EMAIL_QUEUE_ENABLED` gate
remain in place. It now leaves kind `admin_test` rows untouched; those must use
the single-message test path. Do not enable that general worker to activate
transactional mail: it can still select other purposes and kinds.

The separate `process-transactional-email-queue` command accepts only rows
whose purpose is `transactional` and whose kind belongs to the explicit
account-security or order-lifecycle allowlist in
`app/services/email_purpose.py`. Account-security kinds are verification,
login/reset, welcome, account-security, and unsubscribe-confirmation messages.
Order-lifecycle kinds are order receipt/status, payment, cancellation,
completion/refund, and the three trusted Email Studio order notices. Restock,
review-request, marketing-confirmation, all marketing, test, and unknown kinds
are excluded from both queued and retryable-failure selection. Expiry cleanup
and malformed-payload quarantine are scoped to that same allowlist.

The isolated workflow is manual or scheduled every 15 minutes, but remains
inert unless repository variable `EMAIL_TRANSACTIONAL_QUEUE_ENABLED` is exactly
`true`; a manual run also requires `RUN TRANSACTIONAL EMAIL QUEUE`. Its CLI
command independently enforces that confirmation and the same kind/purpose
filter. Leave the new variable unset or `false` until separately approved.
`EMAIL_QUEUE_ENABLED` must remain `false`; the isolated workflow does not need
or change it.

For a provider test, the separate `dispatch-admin-test-email` command accepts
one message ID only. It fails closed unless the row is queued, has kind
`admin_test` and purpose `test`, has no customer account link, contains complete
stored content, its recipient exactly matches `MAIL_TEST_ADDRESS`, and that
address is not attached to any customer account. The claim rechecks these
properties atomically before provider handoff, so two concurrent invocations
cannot send the same row. Failed test messages are not retried by this
one-message command. The manual-only
`.github/workflows/dispatch-admin-test-email.yml` additionally requires
`EMAIL_TEST_DISPATCH_ENABLED=true` and exact confirmation
`RUN SINGLE ADMIN TEST EMAIL`. Configure the GitHub Actions secret
`MAIL_TEST_ADDRESS` privately to the same administrator-controlled address used
by the application; an unset value prevents dispatch. This workflow does not
select, retry, or process any other outbox row.

### GitHub Actions queue workflow

`.github/workflows/process-email-queue.yml` supports manual dispatch and an
optional 15-minute schedule. Both are inert unless repository variable
`EMAIL_QUEUE_ENABLED` is exactly `true`; manual dispatch additionally requires
the confirmation phrase shown by the workflow. Configure it as `false` or leave
it unset until queue deployment, database migration, sender verification, and
controlled tests have all received separate approval. The workflow uses the
Neon PostgreSQL secret only after the identity/schema preflight. It does not
migrate the database. Set the following GitHub repository configuration
privately to the approved Neon production target before separately enabling
the queue. The three processing workflows consume the following configuration:

| Exact name | GitHub location | Requirement / use |
| --- | --- | --- |
| `NEON_DATABASE_URL` | Actions secret | Required by each workflow for the guarded database connection. |
| `FITY_SECRET_KEY` | Actions secret | Required by each production-mode Flask command. |
| `NEON_DATABASE_HOST` | Actions variable | Required exact host identity for preflight. |
| `NEON_DATABASE_NAME` | Actions variable | Required database identity; production value is `neondb`. |
| `NEON_DATABASE_SCHEMA` | Actions variable | Required schema identity; production value is `public`. |
| `MAIL_PROVIDER` | Actions variable | Required; `brevo`, `resend`, or `smtp`. |
| `MAIL_FROM_ADDRESS` | Actions variable | Required verified sender address. |
| `MAIL_FROM_VERIFIED` | Actions variable | Required true only after provider sender verification. |
| `MAIL_FROM_NAME` | Actions variable | Optional display name; code falls back to `FityStores`. |
| `BREVO_API_KEY` | Actions secret | Required only when `MAIL_PROVIDER=brevo`. |
| `RESEND_API_KEY` | Actions secret | Required only when `MAIL_PROVIDER=resend`. |
| `MAIL_SERVER` | Actions variable | Required only when `MAIL_PROVIDER=smtp`. |
| `MAIL_PORT` | Actions variable | SMTP only; defaults to `587` in the workflows. |
| `MAIL_USERNAME` | Actions secret | SMTP only; optional when both username and password are blank. |
| `MAIL_PASSWORD` | Actions secret | SMTP only; optional when both username and password are blank. |
| `EMAIL_QUEUE_ENABLED` | Actions variable | Existing general queue gate; keep unset or `false`. |
| `EMAIL_TRANSACTIONAL_QUEUE_ENABLED` | Actions variable | Transactional worker gate; unset/false is paused. |
| `EMAIL_TEST_DISPATCH_ENABLED` | Actions variable | Single-test workflow gate; unset/false is paused. |
| `MAIL_TEST_ADDRESS` | Actions secret | Required only for the single-test workflow; match the app's administrator-controlled address. |

The GitHub workflows default `MAIL_PORT` to `587`. SMTP encryption defaults in
`config.py` to `MAIL_USE_TLS=1` and `MAIL_USE_SSL=0`; neither is overridden by
these workflows. For SMTP authentication, configure both username and password
or leave both blank. Configure only the credential for the selected API
provider; do not copy secret values into variables or logs.

The isolated transactional workflow additionally uses
`EMAIL_TRANSACTIONAL_QUEUE_ENABLED` (unset/false is paused). The single-admin-
test workflow additionally uses `EMAIL_TEST_DISPATCH_ENABLED` (unset/false is
paused) and Actions secret `MAIL_TEST_ADDRESS`. These new gates do not enable
the general queue or trigger processing by themselves.

The workflows use Ubuntu's `/etc/ssl/certs/ca-certificates.crt` system CA
bundle and enforce `sslmode=verify-full` with hostname verification. The guard
validates that the bundle exists and can be loaded before connecting; it passes
the verified URL and CA settings to later steps through `GITHUB_ENV`.
Do not enable the queue workflow as part of deployment. No always-on polling
worker is needed for the current one-shot interface.

## Migration workflow branch

The manual customer-email schema workflow now targets `main`, which is the
reviewed local branch for this workspace. It remains manual-only, requires
separate operation and backup confirmations, runs identity/schema preflight,
and never migrates automatically. Verify the repository's protected default
and deployment branch before invoking it; this local inspection does not prove
GitHub's current branch protection or deployment configuration.

## Isolated PostgreSQL validation procedure

No database operation is authorized by this runbook. Before the later isolated
test, an operator must verify the Neon Console project ID/name and region for
the restore-test project, and confirm the database endpoint shown by that
project matches the privately configured connection target. Do not use a
database name or role alone as proof. Do not fall back to production credentials.

After that identity is independently confirmed, use only the restore-test
endpoint and perform a read-only first connection. Verify server version,
`current_database()`, `current_user`, `current_schema()`, and the server address
against the expected restore project metadata. If any value differs, stop.
Then use the repository schema guard to inspect tables, columns, constraints,
foreign keys, indexes, sequences, triggers, functions, permissions, and
migration checksums. Compare counts/schema to the restored baseline. Migration
execution remains a later separate approval; this remediation does not run it.

The local identity-pinned preflight can load the dedicated file without
printing its URL. Run this from the repository root only after confirming the
file contains the already verified restore-test endpoint:

```powershell
@'
import os, sys
from dotenv import load_dotenv
if not load_dotenv('.env.neon-test', override=True):
    raise SystemExit('Isolated environment file unavailable.')
os.environ['FITY_NEON_TEST_DATABASE_URL'] = os.environ.get('DATABASE_URL', '')
sys.argv = ['scripts/email_migration_preflight.py']
from scripts.email_migration_preflight import main
raise SystemExit(main())
'@ | python -
```

The script refuses any host/database/role other than its pinned restore-test
identity and runs its report in a PostgreSQL READ ONLY transaction. It does
not load or modify `.env`. Do not save the report in the repository if it
contains internal catalog or operational counts.

The GitHub migration workflow uses the Neon repository secret and variables;
it is not the isolated restore-test runner. Do not dispatch it for isolated
testing or use it before its production-target approval gates are reviewed.

Only after separate approval for schema changes on the isolated test target,
use `scripts/run_isolated_email_cli.py`. It loads `.env.neon-test` in its own
process, verifies the exact host/database/role before creating the Flask app,
enforces `sslmode=verify-full`, and never prints the URL. Its commands are:

```powershell
python scripts/run_isolated_email_cli.py check-customer-email-schema
python scripts/run_isolated_email_cli.py reconcile-customer-email-migration-history --confirm "RECONCILE VERIFIED CUSTOMER EMAIL SCHEMA" --backup-confirm "I HAVE A VERIFIED RESTORABLE BACKUP" # only if needed and after strict schema review
python scripts/run_isolated_email_cli.py migrate-customer-email-schema --confirm "APPLY ISOLATED CUSTOMER EMAIL MIGRATION" --backup-confirm "I HAVE A VERIFIED RESTORABLE BACKUP"
python scripts/run_isolated_email_cli.py check-customer-email-schema
python scripts/run_isolated_email_cli.py migrate-marketing-suppression-schema --confirm "APPLY ISOLATED MARKETING SUPPRESSION MIGRATION" --backup-confirm "I HAVE A VERIFIED RESTORABLE BACKUP"
python scripts/run_isolated_email_cli.py check-marketing-suppression-schema
```

The underlying Flask migration and migration-history reconciliation commands
also require their exact `--confirmation` phrase and
`--backup-confirmation "I HAVE A VERIFIED RESTORABLE BACKUP"` arguments.
Calling those commands without both confirmations stops before database changes.

Do not use the GitHub migration workflow for this isolated target; it is wired
to the Neon production database secret. The exact-target wrapper and a separately
approved operation are prerequisites. Never set or echo the connection URL in
command arguments or logs.

The order-completion concurrency test in
`tests/test_order_completion_concurrency.py` is PostgreSQL-only. It requires
`FITY_ORDER_CONCURRENCY_TEST_DATABASE_URL` to point to a local PostgreSQL
instance (`localhost`, `127.0.0.1`, or `::1`) and a database name ending in
`_test`. It uses two independent sessions and requires PostgreSQL to report
the second session waiting on the order row lock before releasing the first
transaction. Run it with:

```powershell
python -m pytest -q -rs tests\test_order_completion_concurrency.py
```

This focused test covers concurrent completion, stock/reservation updates,
and the single status-email event only. It does not prove PostgreSQL
concurrency for worker claims, suppression versus dispatch, or migration
triggers/indexes/sequences. Those remain unverified until separately
authorized, isolated PostgreSQL integration tests are available. SQLite tests
are not evidence for PostgreSQL row-lock behavior. Do not point the local-only
test at Neon or a production database.

## Render and Brevo administrator checklist

Render settings needing private administrator verification: production
`SECRET_KEY`; intended Neon `DATABASE_URL`; provider selection and credential;
verified sender; web start command; worker runtime secrets; health check; logs;
timeouts and plan support. This repository review cannot confirm dashboard
values or plan capabilities.

In Brevo, verify API access, sender identity, domain authentication, SPF, DKIM,
DMARC, transactional status, webhook URL, configured custom authentication
header, subscribed event types, and webhook retry behavior. The application
does not validate live DNS or provider settings. Brevo's transactional webhook
schema documents `message-id`/`messageId` and event timestamps where available;
its generic unsubscribe event does not itself prove the actor initiated it.
The endpoint therefore conservatively classifies it as an unclassified
provider suppression. See the official [transactional webhook event schema](https://developers.brevo.com/docs/transactional-webhooks)
and [webhook retry behavior](https://developers.brevo.com/docs/retry-mechanism).

The current endpoint accepts `event` or `type` for the event name, and
`email` or `recipient` for the address (or resolves the address from a matched
outbox `message-id`, `messageId`, or `message_id`). Suppression-event
fingerprints require a provider message identifier or event timestamp
(`ts_event`/`ts`), plus normalized event type and recipient; arbitrary top-level
`id` is intentionally not trusted as a unique event ID. The handler recognizes
delivery, bounce, complaint, invalid-address, and unsubscribe event spellings
in `app/public/routes.py`; only hard bounce, invalid address, spam/complaint,
and generic unsubscribe create sticky suppression cases. Soft bounces update a
matched message outcome but do not independently suppress a customer. These are
the fields this code currently consumes, not a claim that every Brevo webhook
configuration emits them. Before production webhook activation, compare a
provider-generated sample from the actual account with this contract.

### Ambiguous-delivery investigation and recovery

An ambiguous outcome is never automatically retried. The admin email screen
offers an authenticated, CSRF-protected investigation flow backed by
`email_delivery_investigation` and append-only
`email_delivery_investigation_event` history. Record the evidence summary,
provider evidence reference, status, and explanation. A message remains
unrecoverable while evidence is inconclusive or provider acceptance is
confirmed. The unique partial index permits only one unresolved investigation
per message; event rows are protected by database triggers against UPDATE,
DELETE, and (on PostgreSQL) TRUNCATE.

Ambiguous message bodies are erased immediately under the retention policy.
Therefore recovery never replays the original payload. Only after an admin
records `CONFIRMED_NOT_ACCEPTED` can an authorized admin compose a new
plain-text replacement. The server renders it through the branded template
(escaping content), preserves the original failed message and attempt, records
the new message link in the investigation history, and creates a new outbox
row. Marketing replacements receive an unsubscribe link and undergo a fresh
account-locked eligibility/frequency check at recovery and again at dispatch.
The dispatch worker writes a linked delivery-attempt record for the replacement.
This action does not contact the provider; it only queues the replacement.

The attempt's committed provider dispatch claim is the point of no return: the
provider request can already be in flight and cannot reliably be recalled.
Never infer non-acceptance from a timeout alone. Use provider logs/evidence
first; if the evidence remains unclear, leave the case unresolved.

Stale-claim recovery marks both the outbox message and its matching
`in_flight` delivery attempt as `ambiguous` in one local transaction, using the
message ID and exact claim token. A claim that has already completed or been
replaced is not changed by recovery. Ambiguous attempts are not automatically
resent.

The explicit customer/email migration now includes two additive migrations:
`20261117_email_admin_test_purpose` corrects legacy `admin_test` purpose rows
without rewriting the historical outbox migration, and
`20261117_customer_consent_audit_immutable` installs append-only protections on
consent events. They are checksum-tracked and run inside the migration's
existing transaction and advisory lock. They have not been run against any
database as part of this local work.

The additive `20261116_email_delivery_ops` migration creates only the four
attempt, investigation, event, and worker-run tables plus their indexes and
append-only trigger. It is included in the existing explicit
`migrate-customer-email-schema` command. The migration checksum and catalog
guard verify these objects; SQLite tests do not establish PostgreSQL trigger,
sequence, transaction, or locking correctness. Run the isolated PostgreSQL
preflight/integration gate described above before treating the migration as
ready for a database operation.

Marketing scheduling remains paused. Transactional queue activation, marketing
activation, migration, deployment, provider settings, and DNS each require
separate explicit approval.
