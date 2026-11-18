# Marketing suppression schema runbook

This runbook is for a future, explicitly approved migration. The migration is
not automatic, and it must be completed and validated before deploying code
that requires the suppression schema. Keep marketing automation paused.

## Required preflight

1. Review and approve the exact code revision containing the migration.
2. Complete a full restore test using a disposable PostgreSQL database matching
   the production PostgreSQL major version. Never restore into production.
3. Confirm a recent recoverable production backup through the approved backup
   process. Do not place credentials in command history or logs.
4. Use an authorized operator environment with the intended database identity
   already configured securely. Do not print connection settings.
5. Run the repository's existing schema preflight and resolve any mismatch
   before proceeding. Production execution requires separate explicit approval.

## Controlled migration commands

From the repository root, with the approved database environment selected:

```powershell
python -m flask --app manage:app migrate-marketing-suppression-schema
python -m flask --app manage:app check-marketing-suppression-schema
python scripts/customer_email_schema_guard.py suppression-check
```

The first command explicitly creates the two suppression tables, their indexes
and append-only event triggers, imports conservative legacy suppression
history, and records a checksum. It uses one PostgreSQL transaction and an
advisory transaction lock. It does not run at web application startup.

The latter commands validate the resulting schema. The standalone schema guard
also applies the repository's protected connection-identity and TLS checks; use
its required settings through the established migration process and never print
secret values. A failed migration should be investigated and retried only after
the failure cause is understood; PostgreSQL transaction rollback leaves no
partially applied schema from that invocation.

## Post-migration checks

- Confirm all three expected tables, named checks, foreign keys (including the
  composite case/account relationship), partial indexes, migration checksum,
  and enabled append-only triggers pass both schema validators.
- Review counts and representative rows for the two suppression tables and
  migration ledger using the approved read-only database procedure.
- Verify existing account, consent, product, order, customer, and administrator
  data remain unchanged using the approved before/after checks.
- Keep scheduled marketing jobs disabled until the separate eligibility and
  automation phases are reviewed and explicitly approved.

No production database access or migration is authorized by this document.

## Marketing dispatch claim semantics

Before a marketing message is handed to a provider, the sender locks the
`customer_account` row using the same `FOR UPDATE` lock used by suppression,
voluntary withdrawal, consent confirmation, administrative clearance and
account activation changes. While holding that lock it rechecks the current
account state and active suppression cases, then changes the queued message
to `sending` in the same transaction. A suppression or withdrawal that commits
first therefore prevents that claim and the provider call. The claim transaction
commits and releases its locks before any provider network request begins.

The commit of `status='sending'` is the point of no return: after it commits,
the provider request may already be in flight and cannot reliably be recalled.
A later suppression prevents later claims, but cannot guarantee cancellation of
a request whose claim already committed. Provider failures retain the existing
failed/retry behavior; duplicate workers cannot claim the same queued message.
# Durable email outbox (Group 1 local implementation)

`queue_email()` now persists a rendered message and returns with status `queued`;
it does not contact SMTP, Resend, or Brevo. Dispatch is a separate explicit
operation through `python -m flask --app manage:app process-email-queue --limit 50 --confirmation "RUN EMAIL QUEUE"`.
No scheduler or Render worker has been configured by this change, so queued mail
will remain queued until an operator-managed worker invokes that command after
deployment prerequisites and the outbox schema have been reviewed.

Each worker uses a conditional database state claim. Marketing claims first lock
the customer account row, re-run centralized eligibility and commit `sending`
with a claim token. That commit is the point of no return: the provider request
may be in flight and cannot reliably be recalled. The account lock is released
before network I/O. Provider results are committed only if the same claim token
still owns the row. A stale claim can be marked for review using
`python -m flask --app manage:app recover-stale-email-claims`; it is recorded as
ambiguous and is never automatically resent.

The queue requires the additive email schema from the existing explicit
`migrate-customer-email-schema` command. This implementation did not run that
command against any database. PostgreSQL migration and concurrent-worker
behavior must be verified against the isolated PostgreSQL 18 restore-test target
before production migration or worker activation. Automation remains paused.

## Group 1.4 email-engine operating notes

### Authentication token supersession

Issuing a verification link, reset link, or login/verification code marks only
the prior unused tokens for the same customer and exact purpose as used. The
token change and outbox insert are committed together. If rendering or outbox
insertion fails, the previous token is still retired in a separate fail-closed
transaction, so a failed send cannot revive it. Token expiry and one-time use
remain enforced. Codes expire after 10 minutes, reset links after 30 minutes,
and verification/preference links after 24 hours.

### Message purpose and frequency

System-generated order and account notices use trusted transactional kinds and
do not consult marketing consent. Every customer-directed, administrator-written
message (including Email Studio service-category content) is marketing. The
`admin_test` preview is restricted to the administrator-entered test address.
No caller can set a frequency override; the legacy column remains for schema
compatibility but is always false and is ignored by final eligibility checks.
Marketing enqueue and final dispatch both enforce the current 24-hour frequency
policy. The final check runs while holding the shared customer row lock.
Concurrent workers serialize there, and only one can claim within the window.

### Queue outcomes and provider responses

The outbox records `queued`, `sending`, `sent`, `failed`, and `suppressed`
separately. A queue insertion is not provider acceptance or inbox delivery.
Successful Resend and Brevo responses persist their provider message ID when
the response includes one; malformed success bodies leave the ID empty. A
confirmed provider throttle (HTTP 429), an explicit transient SMTP rejection,
and an eligibility infrastructure failure are retryable with bounded backoff.
HTTP 5xx/408 responses and transport failures are ambiguous: the provider may
have accepted the request, so those messages are terminal and are not
automatically resent. A customer-ineligibility result is
`suppressed`; database/schema lookup errors fail closed as retryable operational
failures and do not create suppression events. Ambiguous provider outcomes are
not automatically resent.

Rows missing either rendered body become terminal `failed` records with the
stable `malformed_payload` category. The worker quarantines them before choosing
its valid-message batch, so old corrupt rows cannot starve good mail. Their
history remains in `email_message`; the row is not deleted.

### Payload retention

HTML and text bodies are stored in the outbox because a separate process needs
the exact rendered message after the web request ends. These payloads may
contain a one-time code or credential link. Confirmed provider acceptance
clears both bodies while retaining message status, timestamps, recipient,
provider ID and other operational audit metadata. Permanent/ambiguous failures,
malformed rows and stale claims also clear payloads. Retryable provider and
infrastructure failures retain payload only for the bounded retry window (at
most three retries); exhausted retries clear it. On a worker run, queued
authentication messages past their token lifetime are marked failed and erased
instead of being sent after expiry.

If no queue worker runs, queued payloads remain stored until a worker performs
dispatch or expiry cleanup. This is a material operational/privacy dependency.
Encryption-at-rest for application-level bodies, a periodic purge service, and
retention/audit policy for full `email_message` rows would require separately
approved infrastructure and key-management decisions; this code does not add
encryption or delete message history.

### Outbox migration tracking

`migrate-customer-email-schema` applies the outbox change through individual
statements in the same explicit migration transaction. The migration records
the SHA-256 checksum of `migrations/20261113_email_outbox.sql` in
`email_schema_migration`. A rerun validates the recorded checksum and schema,
then skips the backfill. A changed migration file with a previously recorded
checksum fails closed. The backfill only corrects unset, invalid, or known
misclassified purpose values and clears legacy frequency overrides. No table is
dropped or rebuilt. A database CHECK constraint for all message statuses is
deferred because older message histories may contain status values outside the
current worker lifecycle; the guard validates the new purpose/index contract
and preserves those records.

### Worker operating model

No worker or marketing schedule is configured by this change. The existing
one-shot transactional queue operation is:

```text
python -m flask --app manage:app process-email-queue --limit 50 --confirmation "RUN EMAIL QUEUE"
```

The stale-claim review command is:

```text
python -m flask --app manage:app recover-stale-email-claims --age-minutes 30
```

The recovery command marks `sending` rows as ambiguous and never resends them.
An operator must reconcile provider activity before taking any later manual
action. After a process restart, queued rows remain available for the next run;
stale `sending` rows require the explicit recovery review. During a provider
outage, bounded retryable errors remain scheduled and are retried only on a
later queue run after their due time. Permanent and ambiguous errors require
review.

Before selecting a Render operating mode, apply and validate the explicit
customer-email migration using the approved isolated PostgreSQL procedure, then
provide the same required `MAIL_PROVIDER`, verified sender, and provider/SMTP
credentials securely to a separate queue process. The current command is
one-shot, so a Render Cron Job invoking it at a deliberately chosen short
interval is compatible with the existing interface; a continuously running
Background Worker would need a separately implemented polling loop and graceful
shutdown. Keep queue dispatch separate from any future marketing scheduler.
Registration, verification, login-code and reset messages only leave the
outbox once a deliberate queue process is configured and verified. Until then,
they remain queued. Do not treat the queue as operational merely because the
web application can enqueue rows.

The marketing automation entry point remains a paused no-op. Do not enable or
schedule it as part of queue dispatch setup.

### Brevo webhook configuration

The current endpoint is `POST /webhooks/brevo/email-events`. It authenticates
the request by constant-time comparison of the `X-Fity-Brevo-Token` header with
the server-side `BREVO_WEBHOOK_TOKEN` setting. Configure that exact custom
header in Brevo's webhook setup and keep the token in the hosting secret store;
never place it in a public URL or repository. The endpoint is CSRF-exempt only
because it is a provider callback; the shared secret is mandatory.

Brevo's webhook API supports URL authentication and custom headers. Its event
catalogue includes transactional `delivered`, `hardBounce`, `softBounce`,
`blocked`, `spam`, `invalid`, and `unsubscribed` events, among others. Event
payloads can include `messageId` and `ts_event`. Suppression-event
deduplication requires at least one of those stable message/time fields;
descriptive text and the webhook-level `id` alone are not accepted as identity.
Events without either field are quarantined with HTTP 422 for operator review.
The application treats generic
unsubscribe callbacks as unclassified provider suppression; they are not
evidence that the customer initiated the change. Verify the selected webhook
type and actual payload in the Brevo account before enabling a dashboard
configuration. The application does not assume an HMAC signature.
