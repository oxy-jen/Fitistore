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
| Suppression and consent audit events | Do not purge through email payload cleanup or account anonymization | Retain; any deletion policy requires separate privacy/legal review |
| Email message metadata/recipient | This command does not delete or anonymize rows | Account anonymization redacts recipient/content but preserves delivery-attempt metadata |

The 30-day terminal-body period is a maximum for legacy payloads; normal
successful and ambiguous paths erase bodies sooner. Authentication expiry uses
the existing token lifetimes: 10 minutes for one-time codes, 30 minutes for
password reset links, and 24 hours for account verification/preference links.
Retrying messages are protected until their retry window ends. Customer/order
history and suppression/audit rows are outside this cleanup operation.
Rendered bodies are not application-layer encrypted by this code. Provider-
side database encryption and backups must be verified with the database host;
their current settings were not inspected during this local-only task.

## Admin customer account removal

The Registered customers section in the admin email center provides a
CSRF-protected, typed-confirmation account-removal action. It disables sign-in,
replaces the account email/name with non-routable redacted values, clears
password/Google identity, verification tokens, and the saved cart, and removes
the old referral code from active use. The account row remains as an anonymous
tombstone so existing foreign keys and audit relationships remain valid; this
does not require or run a migration. Inactive customer sessions are rejected
by the user loader.

The action suppresses queued and retryable messages for the account, redacts
message recipients and bodies, and retains delivery attempts and provider
outcome metadata. If a message is already in `sending` state or has an active
claim, removal stops without changing data; wait for that provider attempt to
finish and retry. Ambiguous delivery outcomes remain ambiguous and are not
requeued. Campaign-recipient addresses are redacted and queued recipients are
suppressed.

Optional checkboxes select permanent deletion of linked orders/payment
records, payment proof files, public reviews, referral/loyalty history,
product-exposure history, account-addressed email drafts, and email-matched
product enquiries/restock alerts. Unselected enquiries have contact details
redacted; pending restock alerts are cancelled and their contact details are
redacted so a later inventory event cannot requeue them.
Consent and suppression audit records are always retained, with free-text PII
redacted; deleting those records requires a separately reviewed privacy/legal
retention policy. Review text remains public unless its deletion checkbox is
selected. Check applicable payment, tax, and record-retention obligations
before selecting order or audit data for deletion.

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

### Email Studio and legacy manual marketing sends

Email Studio continues to support transactional order notices and test
previews. Marketing sends/schedules through Email Studio and the old manual
promotional/customer-service endpoints are intentionally blocked: those
messages are not campaign-linked and are excluded from both the general and
isolated campaign workers. Use the Marketing campaign centre to draft, preview,
snapshot eligible recipients, and schedule a marketing campaign. This avoids
leaving legacy marketing rows queued without a worker that is allowed to
process them. Saved templates and drafts remain available for reuse.

### Automatic campaign draft preparation

The `prepare-marketing-campaign-drafts` command is a separate, review-only
preparation path. It does not create recipients, queue `EmailMessage` rows,
approve campaigns, schedule campaigns, or contact Brevo. The command and its
GitHub Actions job both fail closed unless
`EMAIL_MARKETING_PREPARATION_ENABLED=true`; the default is false. The
preparation gate is independent of `EMAIL_MARKETING_QUEUE_ENABLED` and the
campaign-type dispatch gates.

The existing `.github/workflows/process-marketing-email-queue.yml` polls every
15 minutes. Its independent `prepare` job uses the same cadence only when the
preparation gate is enabled. New-arrival candidates are active, in-stock
products created or positively restocked within the configured
`EMAIL_MARKETING_NEW_ARRIVAL_LOOKBACK_DAYS` window (default 7 days, bounded to
1–30). Abandoned-cart candidates must have a valid available item, be past
`EMAIL_MARKETING_ABANDONED_CART_IDLE_MINUTES` (default 180, bounded to 5–10,080
minutes), have no active checkout, pass centralized marketing eligibility,
and have no matching completed/cancelled order placed after the cart update.
The weekly draft is unique per ISO week and uses recorded paid completed sales
and cart additions when present; otherwise it uses currently available
catalogue items without claiming they are popular.

Automatic drafts appear in the existing Marketing campaigns list with their
source and reason. An administrator can edit, preview, schedule manually or
discard them. New-arrival/product and weekly-picks keys are unique per product
and ISO week; abandoned-cart keys are unique per cart episode. Discarded drafts
retain their unique key so the same source event is not silently regenerated.
Abandoned-cart preview content is generic and contains no customer name,
address or cart-specific recipient details. The generated draft targets that
cart episode when scheduled; current eligibility, cart contents and idle
threshold are rechecked during the audience snapshot. PostgreSQL behavior for
simultaneous preparation is enforced by the unique partial index, but must be
validated against the isolated PostgreSQL database before enabling the gate.

The additive `20261121_marketing_campaign_draft_preparation` migration adds
nullable generator metadata to `email_campaign` and a unique partial index.
It preserves existing campaign rows. It has not been run against production
or any database during local implementation. The explicit existing
`migrate-customer-email-schema` command includes it under the existing
transaction and migration-lock path; do not activate preparation until the
isolated migration and schema-guard checks pass.

Preparation configuration is deliberately separate from campaign delivery:

| Setting | Location | Default / use |
| --- | --- | --- |
| `EMAIL_MARKETING_PREPARATION_ENABLED` | Render environment and GitHub Actions variable | `false`; must be explicitly enabled in each context. This creates drafts only. |
| `EMAIL_MARKETING_NEW_ARRIVAL_LOOKBACK_DAYS` | GitHub Actions variable (optional); Render environment for matching admin status | `7` days, bounded to 1-30. |
| `EMAIL_MARKETING_ABANDONED_CART_IDLE_MINUTES` | GitHub Actions variable (optional); Render environment for matching admin status | `180` minutes, bounded to 5-10,080. |

The workflow uses the defaults above when optional Actions variables are unset.
Leave the preparation gate unset or `false` until the additive migration has
been applied and validated in the isolated PostgreSQL restore. This gate does
not enable the marketing dispatch gate or any campaign-type gate.

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

Short-lived `verify_code` and `login_code` rows are also attempted immediately
after their outbox transaction commits, through the same transactional allowlist,
provider checks, and atomic delivery claim used by the worker. The claim locks
the linked customer account and revalidates the active account, recipient,
verification state, one-time token purpose, token use state, and expiry before
provider handoff. Challenge issuance takes the same account lock, so a replaced
code cannot be newly claimed. If provider configuration is unavailable during
immediate delivery, the message is marked failed and its short-lived payload is
cleared; that failed message is not left queued for the worker. For a Brevo
HTTP 429 response, the system reads the provider's `Retry-After` guidance and
records the suggested time for customer messaging, marks the authentication
delivery failed, clears the short-lived payload, and does not automatically
resend that code. The customer is asked to wait for the provider cooldown and
request a fresh code. Ambiguous provider outcomes are never retried
automatically. The scheduled worker remains a fallback only for eligible
messages that are still queued, and it rejects codes whose linked token has
expired.
The committed delivery claim is the point of no return: provider I/O may be in
flight after the database lock is released and cannot reliably be recalled. The
15-minute scheduler is a fallback for eligible messages that remain queued,
not a guarantee of delivery by a code's expiry. This request-time transactional
path is independent of marketing consent and does not enable the separately
gated workers.

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
the queue. The queue and single-message test workflows consume the following
configuration:

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

### Isolated marketing campaign workflow

The admin campaign centre creates drafts, previews, snapshots eligible
recipients, and supports pause/resume/cancel. Scheduling requires an exact
`SCHEDULE N` confirmation for the current eligible-recipient count, a prior
preview, the global application gate, and the selected campaign-type gate.
Campaign messages are linked to a campaign and account; manual/legacy marketing
messages are not selected by this worker. The worker rechecks consent,
verification, suppression, unsubscribe history, frequency, campaign status,
schedule time, and the type gate immediately before each provider request.
Paused, cancelled, or not-yet-due campaigns remain queued without being
suppressed by a dispatch race. Retry selection, malformed-payload quarantine,
and dispatch claims are all campaign-scoped.

The dedicated `.github/workflows/process-marketing-email-queue.yml` workflow
uses the same verified Neon identity preflight and provider credentials as the
other queue workflows. It runs only on `main` and only when repository variable
`EMAIL_MARKETING_QUEUE_ENABLED` is exactly `true`; manual runs also require
`RUN MARKETING EMAIL QUEUE`. The scheduled trigger is inert unless that same
repository variable is deliberately enabled. The command independently checks
the exact confirmation and application environment gate. Keep the queue gate
and every type gate unset or `false` until campaign schema, sender, consent
eligibility, and a single controlled test plan are approved.

### Marketing schedule and preparation model

The existing low-cost dispatch cadence is every 15 minutes. A scheduled run
only dispatches rows whose campaign is approved, scheduled, due, still enabled
by its campaign-type gate, and eligible at dispatch time. GitHub scheduled
events are best-effort, so delivery may occur after the selected time; the
cadence is a polling interval, not a send-time guarantee. With the master gate
unset or `false`, the job is skipped before a runner starts.

| Work | Current processing | Recommended cadence |
| --- | --- | --- |
| Approved scheduled campaign dispatch | `process-marketing-email-queue` | Every 15 minutes; already configured. |
| Abandoned-cart draft preparation | The gated preparation job finds idle, currently eligible carts and creates generic, unscheduled `abandoned_cart` drafts targeted to a cart episode. Admin review is required; eligibility/cart state is checked again at audience snapshot and dispatch. | Every 15 minutes in the existing workflow only when `EMAIL_MARKETING_PREPARATION_ENABLED=true`; draft generation is separately default-off. |
| Weekly product recommendations | The gated preparation job creates at most one `weekly_picks` draft per ISO week using recorded paid sales/cart additions, or available catalogue items when no reliable signal exists. Admin can edit/replace products and must preview/schedule. | Checked every 15 minutes by the existing workflow; uniqueness prevents more than one draft per week. |
| New-arrival campaign preparation | Active, in-stock products created or positively restocked within the configured lookback are prepared as `new_product` drafts using stored product data. Admin can edit and preview. | Checked every 15 minutes by the existing workflow; default lookback is 7 days. |

This arrangement prepares review-only drafts while keeping content approval,
scheduling and delivery human-controlled. The legacy
`run-marketing-automation` command and `.github/workflows/marketing-automation.yml`
are paused/no-op; do not schedule or enable them. The old
`MarketingAutomationSettings` table is not the control plane for either the
campaign worker or draft-preparation job, and no admin settings screen currently
configures its legacy daily or weekly hours. Do not revive the legacy direct-send
path.

| Exact name | Location | Default / purpose |
| --- | --- | --- |
| `EMAIL_MARKETING_QUEUE_ENABLED` | Render application environment and GitHub Actions variable | `false`; master scheduling/worker gate. |
| `EMAIL_MARKETING_NEW_PRODUCTS_ENABLED` | Render application environment and GitHub Actions variable | `false`; new-product campaigns. |
| `EMAIL_MARKETING_OFFERS_ENABLED` | Render application environment and GitHub Actions variable | `false`; special-offer campaigns. |
| `EMAIL_MARKETING_WEEKLY_PICKS_ENABLED` | Render application environment and GitHub Actions variable | `false`; weekly-picks campaigns. |
| `EMAIL_MARKETING_ABANDONED_CART_ENABLED` | Render application environment and GitHub Actions variable | `false`; each recipient's own active cart only. |
| `EMAIL_MARKETING_SEASONAL_ENABLED` | Render application environment and GitHub Actions variable | `false`; seasonal campaigns. |
| `EMAIL_MARKETING_CUSTOM_ENABLED` | Render application environment and GitHub Actions variable | `false`; custom campaigns, independent of seasonal campaigns. |

The campaign schema change is additive and is part of the existing explicitly
confirmed customer-email migration path. It is not run by the queue workflow or
web application startup. Verify the migration checksum and schema guard before
separately approving it for a target database. `run-marketing-automation`
remains a compatibility no-op; the campaign worker is the explicit
`process-marketing-email-queue` command. Neither command changes or enables the
legacy marketing automation settings.

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

Before an isolated test, verify the Neon Console project ID/name and region for
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
execution remains conditional on a successful identity and schema preflight and
is limited to the explicitly approved isolated target; this runbook does not
execute it.

The local identity-pinned preflight targets the verified Neon restore-test
host, role `neondb_owner`, database `backup_restore_check`, schema `public`,
and PostgreSQL major 18. The isolated URL must name `backup_restore_check`;
the previous `neondb` target is rejected. Run this from the repository root
only after confirming the private URL matches the restore-test endpoint:

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

The owner has approved applying the campaign draft preparation migration only
to the isolated `backup_restore_check` database for this validation task. Use
`scripts/run_isolated_email_cli.py` after the preflight reports that it is the
only missing migration. It loads `.env.neon-test` in its own
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
# Recovering a campaign rejected by Brevo configuration

The campaign detail page offers **Recover confirmed Brevo rejections** only for
a partially failed custom campaign. It counts only existing message/recipient
links whose stored outcome is exactly `Brevo HTTP 401.`, with one failed Brevo
attempt and no accepted or ambiguous attempt. This is a confirmed provider
rejection, not a delivery investigation or an automatic retry.

After correcting and verifying the Brevo configuration, an administrator can
review the original campaign and use this action. Enter `RECOVER N`, where
`N` is the displayed candidate count, and select a time at least 30 minutes in
the future. The action reuses the same outbox messages, recipient rows and
idempotency keys. It creates no recipients and makes no provider request. It
rechecks consent, subscription, verification, suppression, frequency and the
account's current email while holding the marketing account lock. Recipients
who are no longer eligible are suppressed instead of requeued. The worker
repeats the full dispatch eligibility check later.

If a message body was cleared, the system re-renders it from the saved campaign
subject, body and product IDs, using current product records and a fresh
unsubscribe token. This restores missing content without inventing it, but a
product's current name, price, availability or image may differ from its value
when the first attempt was prepared. Review the campaign preview and product
data before authorizing recovery.

After scheduling, verify that every intended message is `queued`, the matching
campaign recipient row is also `queued`, the campaign is `scheduled`, and each
message has non-empty subject/HTML/text, the original account and recipient
link, the original campaign idempotency key, and the intended future
`next_attempt_at`. Check any suppressed recipients and their reason. Only then
allow the separately gated marketing worker to run after the scheduled time.
The action is audited in the delivery-investigation event ledger and links the
next provider attempt to the confirmed rejected attempt.
