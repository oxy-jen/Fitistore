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
provider setup:

```powershell
python -m flask --app manage:app process-email-queue --limit 25 --max-seconds 240
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

### GitHub Actions queue workflow

`.github/workflows/process-email-queue.yml` supports manual dispatch and an
optional 15-minute schedule. Both are inert unless repository variable
`EMAIL_QUEUE_ENABLED` is exactly `true`; manual dispatch additionally requires
the confirmation phrase shown by the workflow. Configure it as `false` or leave
it unset until queue deployment, database migration, sender verification, and
controlled tests have all received separate approval. The workflow uses the
production database secret only after the identity/schema preflight. It does
not migrate the database. Ensure these repository variables/secrets are set
privately and match the approved production target before enabling the queue:

- Secret: `RENDER_DATABASE_URL`.
- Variables: `RENDER_DATABASE_NAME`, `RENDER_DATABASE_SCHEMA`,
  `RENDER_DATABASE_HOST`.
- Secret: `RENDER_DATABASE_CA_CERT_PEM`.
- Variable: `EMAIL_QUEUE_ENABLED` (default/unset is paused).
- Secret: `FITY_SECRET_KEY`.
- Mail provider variables and the corresponding provider credential secrets.

Do not enable the workflow as part of deployment. Render plan capabilities and
cost were not available for inspection. A Render Cron Job is compatible with a
one-shot command if the chosen plan supports it; otherwise this guarded GitHub
Actions option is available after approval. No always-on polling worker is
needed for the current one-shot interface.

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

The existing GitHub migration workflow is wired to the Render database secret;
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

Do not use the GitHub migration workflow for this isolated target; it is wired
to the Render database secret. The exact-target wrapper and a separately
approved operation are prerequisites. Never set or echo the connection URL in
command arguments or logs.

Focused PostgreSQL concurrency validation must use two independent connections
to that isolated database and cover transaction advisory locking, shared
customer-row locks, suppression/withdrawal versus dispatch, worker claims,
claim-token fencing, trigger enforcement, valid indexes, sequence defaults,
rollback, and reruns. SQLite tests are not evidence for these PostgreSQL
behaviors.

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
