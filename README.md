# FityStores Flask Shop

FityStores is a mobile-first Flask e-commerce starter for a small Kenyan merchandise shop. It uses server-rendered pages, SQLite, local image uploads, owner-managed prices and stock, and WhatsApp as the customer enquiry channel.

The app is intentionally honest about manual selling: a WhatsApp message is an enquiry, not proof of payment, delivery, or confirmed stock.

## Architecture

```text
FityStores/
  app/
    __init__.py              Application factory, CLI setup, error handlers
    extensions.py            SQLAlchemy, LoginManager, CSRF extension objects
    models.py                Database tables and relationships
    public/routes.py         Home, shop, product detail, cart, checkout, reviews, SEO files
    admin/routes.py          Login, dashboard, products, inventory, orders, reviews, settings
    services/                Cart validation, order creation, inventory, formatting, WhatsApp links
    templates/               Jinja2 pages and partials
    static/css/style.css     FityStores design system
    static/js/               Mobile menu and localStorage cart
    static/images/           Local logo and placeholder artwork
    static/uploads/          Local development product image uploads
  tests/                     Pytest tests
  config.py                  Development and test configuration
  requirements.txt           Python dependencies
  wsgi.py                    Production entry point
```

## Database Design

Main models:

- `AdminUser`: protected owner account with hashed password.
- `Category`: owner-managed catalogue groups seeded with FityStores categories.
- `Product`: product details, price in cents, previous price, stock, low-stock threshold, active/featured flags, sizes and colours.
- `ProductImage`: uploaded image metadata.
- `ProductVariant`: future-ready size/colour stock records.
- `InventoryMovement`: stock adjustment audit trail.
- `Order` and `OrderItem`: saved checkout enquiry with price snapshots.
- `Review`: moderated customer review submissions.
- `ShopSettings`: WhatsApp number, collection, delivery, payment, and display settings.

Money is stored as integer cents to avoid floating-point rounding problems.

## Windows PowerShell Setup

```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` and set a private `SECRET_KEY`, `ADMIN_USERNAME`, and `ADMIN_PASSWORD`. Add `SHOP_WHATSAPP_NUMBER` only when the owner has a real business number in international format.

Initialize the database:

```powershell
flask --app wsgi.py init-db
```

Create an admin manually instead of storing a password in `.env`:

```powershell
flask --app wsgi.py create-admin owner
```

Run locally:

```powershell
flask --app wsgi.py run
```

Open `http://127.0.0.1:5000`.

## Testing

```powershell
pytest
```

The tests use an in-memory SQLite database and separate sample data. They are not real orders or real sales.

## Product Images

Product image uploads use Cloudinary in production. Legacy sample images and local development files may still live in `app/static/uploads`, but ordinary Render files are not durable across deploys or restarts. Do not upload private address information or payment screenshots.

## Deployment Notes

Free-tier hosting changes over time, so verify current limits before choosing a provider. Look for support for Python apps, environment variables, persistent database storage, and persistent uploaded media. SQLite is acceptable for local learning and very low traffic, but PostgreSQL is the safer path if orders and concurrent admin activity increase.

Never enable debug mode in production, never commit `.env`, and back up the database before changes.

## Implementation Checklist

- Complete app factory and module structure: done.
- Owner-controlled products, prices, stock, images: done.
- Public home, shop, product detail, cart, checkout, contact, about, reviews: done.
- Server cart validation and encoded WhatsApp links: done.
- Admin login, products, inventory, orders, review moderation, settings: done.
- No fake products, prices, stock, reviews, or phone number: done.
- Tests for public pages, filtering, cart validation, WhatsApp, admin auth, stock, reviews: done.
- Production deployment still requires owner configuration and host-specific testing.


## Catalogue and Inventory Upgrade

Run the initializer after pulling these changes so existing SQLite databases receive the `sku` column, starter categories/products, and local sample product artwork:

```powershell
flask --app wsgi.py init-db
```

The starter catalogue uses confirmed prices only: bonnet KSh 120, ankle socks KSh 40, and hair bands at KSh 10, KSh 50, and KSh 60. Shower caps, underwear/panties, vests, boxers, and shoes are created as inactive owner-completion drafts so their prices are not invented.

Admin product management is available at `/admin/products`. Administrators can search/filter inventory, add products, upload one or more product images, preview selected images, choose a primary image, remove images, update stock, mark products sold out, hide products, or delete products with confirmation.

The shop page uses `/api/products` for paginated infinite scrolling. Search, category filters, and sorting are preserved while more products load.
## Cloudinary Product Images

Production product photos are stored in Cloudinary so they survive Render deploys and restarts. Create a Cloudinary account, open the dashboard, and copy the cloud name, API key, and API secret into Render environment variables:

```text
CLOUDINARY_CLOUD_NAME
CLOUDINARY_API_KEY
CLOUDINARY_API_SECRET
```

Keep these values private. Do not commit them to GitHub or paste them into issue threads.

Set the Render Start Command to:

```bash
gunicorn wsgi:app
```

Do not use `flask --app wsgi init-db && gunicorn wsgi:app` in production. `init-db` is only for local SQLite setup, and production migrations should be run as explicit one-off commands so startup never seeds or resets production data.

The app needs two compatible columns on `product_image`: `storage_backend` and `cloudinary_public_id`. Existing filename records remain readable. New admin uploads and online image imports require Cloudinary to be configured; otherwise the admin receives a clear error and the product is not falsely saved with a missing image.

Production PostgreSQL schema changes are not run automatically during Gunicorn startup. After backing up the Render database and deploying the code, apply the focused Cloudinary schema migration once. The migration is idempotent and safe to rerun:

```bash
cd /opt/render/project/src && python -m flask --app wsgi:app migrate-cloudinary-schema
```

If Render Shell is not available, run the SQL in `migrations/20261013_cloudinary_product_image_columns.sql` through Render's PostgreSQL query console or another trusted PostgreSQL client connected to the production database:

```sql
ALTER TABLE product_image
  ADD COLUMN IF NOT EXISTS storage_backend VARCHAR(20) NOT NULL DEFAULT 'local';

ALTER TABLE product_image
  ADD COLUMN IF NOT EXISTS cloudinary_public_id VARCHAR(255);
```

Do not run `init-db` against production PostgreSQL. It is reserved for local SQLite setup.

To migrate legacy local image files that are still present on the machine running the command, run this explicit one-off command after the schema migration:

```bash
cd /opt/render/project/src && python -m flask --app wsgi:app migrate-product-images
```

The migration skips sample images and records already migrated, uploads only files it can actually read from `UPLOAD_FOLDER`, and updates each database record only after Cloudinary upload succeeds. It does not delete local originals. If Render no longer has the old uploaded files, the original image bytes cannot be recovered from the saved filename alone; those products remain identifiable in the command output and should be re-uploaded manually from the admin product form.
If product photos were migrated on a local database but the live Render site still shows sample SVGs, the Render PostgreSQL rows probably still lack `cloudinary_public_id` values. A local migration does not update the production database. Use an explicit mapping backfill instead of seeding or resetting production:

1. On the database that already has the correct Cloudinary IDs, export a mapping:

```powershell
python -m flask --app wsgi:app export-product-image-map --output product-image-map.csv
```

2. Put that mapping file somewhere available to the Render environment or run the command from a trusted machine configured with Render's production `DATABASE_URL` and Cloudinary env vars. First run a dry run:

```bash
python -m flask --app wsgi:app backfill-cloudinary-image-ids --mapping product-image-map.csv --ignore-filename
```

3. If the dry run reports the expected rows and no unexpected failures, apply it:

```bash
python -m flask --app wsgi:app backfill-cloudinary-image-ids --mapping product-image-map.csv --ignore-filename --apply
```

The backfill command updates only `product_image.cloudinary_public_id` and `product_image.storage_backend`. It does not upload assets, delete records, seed products, change prices, change stock, or touch orders/customers. It is idempotent: already-mapped rows are skipped. Use `--ignore-filename` when production still has `sample-*.svg` filenames but the mapping came from a local database with real uploaded filenames. If a mapping row matches multiple production image rows, it fails that row instead of guessing.


Before applying the PostgreSQL schema migration in Render, create or confirm a recent database backup in the Render dashboard. Record the backup timestamp. After the migration, verify the two columns exist without inspecting sensitive data:

```sql
SELECT column_name, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'product_image'
  AND column_name IN ('storage_backend', 'cloudinary_public_id')
ORDER BY column_name;
```

Expected result: `storage_backend` is `NO` for nullable with a default containing `local`; `cloudinary_public_id` is nullable. You can also verify existing rows were preserved with:

```sql
SELECT COUNT(*) FROM product_image;
```

Compare the count with the count before the migration if you captured it during backup verification.

To verify deployment, open `/admin/products`, edit a product, upload a small jpg/png/webp/gif, save, and confirm the thumbnail appears in the admin list, home/shop cards, and product detail page. If migration or upload fails, keep the database as-is, check the Render environment variables, rerun `migrate-product-images` for legacy files, or re-upload the affected product photos manually.

## Customer accounts and email

The existing `AdminUser` table and admin authentication remain separate. Customer accounts use `CustomerAccount`; customer sessions have prefixed IDs and the admin blueprint rejects customer identities. Existing guest checkout and WhatsApp ordering remain available. Only orders submitted while signed into a verified customer account are linked to that account. Older guest orders are not automatically claimed by email or phone matching.

### Database setup

Creating the Flask app does not create or alter database tables. For a new local SQLite store, run `python -m flask --app wsgi:app init-db` explicitly; this command can seed starter data and is for a fresh local store only. The customer/email migration creates only the twelve customer/account email tables and adds the required account, outbox, and retry fields. Before changing an existing database, inspect it with `python -m flask --app wsgi:app check-customer-email-schema`. This command reads schema metadata and reports missing tables, columns, indexes, uniqueness constraints, and foreign keys without changing the database. The migration checks required base tables and validates the resulting schema; SQLite uses a transaction, while PostgreSQL uses an advisory lock and transaction. An incomplete existing feature table causes an error rather than a destructive repair.

For PostgreSQL, first confirm the active `DATABASE_URL` points to the intended database, take and verify a restorable backup, and review the migration SQL. Then apply the additive migration explicitly:

```powershell
python -m flask --app wsgi:app migrate-customer-email-schema
```

The migration adds customer identity, token, consent, order-link, rate-limit, campaign, and email-message tables. It does not change or delete existing order, product, inventory, payment, image, or admin records. Run it before enabling customer account/email routes on PostgreSQL. The migration is idempotent. Referral, review, and cart-counter database changes are separate and use `migrate-postgresql-feature-schema`; Cloudinary image columns use `migrate-cloudinary-schema`.

For the hosted PostgreSQL database when Render Shell is unavailable, the repository includes a manual GitHub Actions workflow at `.github/workflows/migrate-customer-email-schema.yml`. On the repository's default branch, first save Render's **external** PostgreSQL URL as the GitHub Actions secret `RENDER_DATABASE_URL` (never paste it into a file or chat). Open **Actions > Migrate customer email schema > Run workflow**, type exactly `MIGRATE CUSTOMER EMAIL SCHEMA`, and start it. It performs a read-only preflight, runs only the additive customer/email migration, then fails unless the post-migration schema check is complete. It does not send emails. The workflow must be on the default branch before it can be manually run.

#### Production database safety

Do not run a migration until you have confirmed the destination database and verified a backup. Never paste a database URL or password into a ticket, terminal transcript, or source file. For a Render PostgreSQL database, run `pg_dump` from a trusted computer using the database's external host, port, database name, and username; allow `pg_dump` to prompt for the password. Save the custom-format backup outside the repository and verify it with `pg_restore --list` before proceeding:

```powershell
pg_dump --host YOUR_RENDER_EXTERNAL_HOST --port YOUR_RENDER_PORT --username YOUR_DATABASE_USER --dbname fity --format=custom --file fity-before-schema-change.dump
pg_restore --list fity-before-schema-change.dump
```

The Render Free PostgreSQL database used by this project is scheduled to expire on November 12, 2026. Treat that date as a data-retention deadline: make and verify an independent backup and arrange an approved database migration before expiry. Do not use `init-db` on a production database. Run the read-only preflight against the target first, take the backup, then run only the explicitly approved migration. Afterward, run the preflight again and confirm all required tables and constraints are complete. No production migration or backup is performed by this repository documentation.

### Email provider setup

Configure a transactional provider and a sender address that the provider has verified. For Render Free, use Brevo or Resend over HTTPS; Render Free blocks outbound SMTP ports 25, 465, and 587. The application supports `MAIL_PROVIDER=brevo`, `MAIL_PROVIDER=resend`, and `MAIL_PROVIDER=smtp`:

```text
MAIL_PROVIDER=brevo
BREVO_API_KEY=<Brevo API key stored as a secret>
RESEND_API_KEY=<only needed when MAIL_PROVIDER=resend>
MAIL_FROM_ADDRESS=<provider-verified sender>
MAIL_FROM_NAME=FityStores
MAIL_REPLY_TO=<real inbox the business monitors>
MAIL_SERVER=<provider SMTP host>
MAIL_PORT=587
MAIL_USERNAME=<provider username>
MAIL_PASSWORD=<provider password>
MAIL_USE_TLS=1
MAIL_USE_SSL=0
MAIL_FROM_VERIFIED=1
MAIL_TEST_ADDRESS=<authorized owner test inbox>
PUBLIC_BASE_URL=https://fitystore.top
```

For MAIL_PROVIDER=brevo, only BREVO_API_KEY and the verified sender settings are needed; this sends through Brevo HTTPS API. MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD, MAIL_USE_TLS, and MAIL_USE_SSL can remain unset or unchanged because Brevo does not use SMTP. RESEND_API_KEY is needed only for MAIL_PROVIDER=resend. Resend remains available with MAIL_PROVIDER=resend and RESEND_API_KEY. If using SMTP on a host that allows it, choose exactly one encrypted transport: MAIL_USE_TLS=1 for STARTTLS (typically port 587), or MAIL_USE_SSL=1 for implicit TLS (typically port 465). Never enable both or disable both. Set MAIL_FROM_VERIFIED=1 only after the provider verifies the sender/domain. support@fitystore.top, orders@fitystore.top, and hello@fitystore.top are naming examples only; this project does not create mailboxes, aliases, or forwarding. A sender identity can send mail but does not itself receive replies. Configure MAIL_REPLY_TO to an inbox that exists and is monitored. Buying the domain alone does not include email hosting. Set PUBLIC_BASE_URL=https://fitystore.top in Render so verification links and Google callbacks use the canonical HTTPS host regardless of proxy headers. After environment changes, save and redeploy or restart the web service.

Follow the provider's current DNS instructions to publish SPF and DKIM. Add a DMARC policy for `fitystore.top` and monitor aggregate reports before tightening enforcement. DNS is not changed by this application. Provider plans, free quotas, costs, and setup requirements change; check the selected provider before relying on a free tier.

The admin Customer email page shows whether the selected email provider is configured, subscriber consent, campaign history, and the last 30 message statuses. A `sent` status means the selected provider accepted the message; it does not prove inbox delivery. The authenticated Brevo webhook can update matching outbox and campaign-recipient records to delivered, bounced, or complained; hard bounces, invalid addresses, and complaints suppress marketing. Soft bounces are recorded as bounced but do not automatically suppress. Webhook events are safe to repeat. There is no automatic retry worker. Email failures do not roll back committed orders or payment records. Campaign sending is synchronous and intended for low volume; large lists may exceed a web-host request timeout. Send a test message to the configured test address before running a campaign.

### Google sign-in setup

Install the pinned dependencies from requirements.txt, create an OAuth Web application client in Google Cloud, and place its client ID in GOOGLE_CLIENT_ID in Render. The callback is built from PUBLIC_BASE_URL plus the /account/google route. With the production setting, configure this exact origin and redirect URI:

Authorized JavaScript origin: https://fitystore.top
Authorized redirect URI: https://fitystore.top/account/google

The Google link flow uses a JavaScript callback and posts the credential to /account/google/link; it does not use another Google redirect URI. If PUBLIC_BASE_URL or GOOGLE_CLIENT_ID changes, update Google Cloud to match and redeploy or restart Render.

Also authorize `fitystore.top` in the Google consent/branding configuration. No client secret is used by this Google Identity Services ID-token flow; do not add one to the app. The server verifies the ID token signature, audience, issuer and expiry with `google-auth`, checks `email_verified`, and uses `sub` as the Google identity. Automatic Google registration is limited to Gmail or verified Google Workspace addresses. Existing email accounts are never silently linked; the customer must sign in first and link the matching verified Google address from the account page.

The integration stays unavailable until `GOOGLE_CLIENT_ID` and the `google-auth` package are configured. Google setup/reference: <https://developers.google.com/identity/gsi/web/guides/verify-google-id-token>.

### Safe manual test

1. In a test/local environment, leave real SMTP credentials empty and keep the test database. The app records email as `not_configured` instead of sending it.
2. Register a customer with an unused email and a 12+ character password. Verify the stored password is a hash and the marketing checkbox starts unchecked.
3. In the test database only, inspect the `CustomerToken` hash and use a test-created raw token to POST the verification form. Confirm expiry and single-use behavior.
4. Check that marketing status is `none` when unchecked. If opting in, confirm the separate `marketing_confirm` token only after verifying the account, then unsubscribe using the one-time link.
5. Sign into a verified account before checkout and confirm its order appears on that account only. Submit a guest order and confirm it remains available through the existing WhatsApp/tracking workflow without an account link.
6. To exercise email delivery in tests, mock the provider transport (`app.services.customer_mail._send_smtp`, `_send_resend`, or `_send_brevo`) or mock `urllib.request.urlopen`. Automated tests must never use a live provider credential.
7. Only after sender verification, configure `MAIL_TEST_ADDRESS`, send one test message from the admin centre, and inspect its status. Preview a campaign and type `SEND` only when the eligible subscriber count and content are correct.

Use HTTPS in production and set `SESSION_COOKIE_SECURE=1`. Registration, login, verification, password reset and Google sign-in are rate-limited per IP/action; session cookies remain HTTP-only and SameSite Lax. Password reset/verification raw tokens are only included in the generated email and are stored in the database as SHA-256 hashes. The email outbox stores recipient, subject, event, status and failure summary, not token links or message bodies. Review privacy wording and any retention schedule with the business owner; this implementation is not a legal compliance certification.


## Scheduled marketing automation

Marketing automation is opt-in at both levels: customers explicitly request and confirm promotional email, and the owner enables schedules in Admin > Customer email > Email automation. Account terms remain separate from marketing consent. Essential verification, security, and order messages continue independently.

The existing migrate-customer-email-schema command now also creates the signed-in cart snapshot and automation run/settings tables and additively adds the outbox retry counter. Run it once after deploying this version, against the same database used by the web service:

    python -m flask --app wsgi:app migrate-customer-email-schema

The automation command runs independently of web traffic:

```powershell
python -m flask --app wsgi:app run-marketing-automation
```

Render Cron Jobs have a $1/month minimum, so they are not a zero-cost option. A free, best-effort alternative is the guarded GitHub Actions workflow at `.github/workflows/marketing-automation.yml`. It runs every six hours. It is disabled unless the repository variable `RUN_MARKETING_AUTOMATION` is set to `true`, and the database's admin automation setting must also be enabled. A six-hour cadence can delay abandoned-cart reminders by up to six hours, and GitHub may delay scheduled runs. GitHub disables scheduled workflows in public repositories after 60 days without repository activity.

Before enabling it, confirm the production PostgreSQL schema is complete and you have a backup. The workflow never runs migrations. Add these GitHub Actions secrets without putting their values in the repository: `RENDER_DATABASE_URL` (the external PostgreSQL URL), `APP_SECRET_KEY`, `BREVO_API_KEY`, `MAIL_FROM_ADDRESS`, and `MAIL_REPLY_TO` (optional). Then set the repository variable `RUN_MARKETING_AUTOMATION=true`. The database automation setting remains a second pause switch. Do not add `DATABASE_URL` to the source or workflow file.

GitHub Actions standard runners are free for public repositories; GitHub Free includes 2,000 minutes monthly for private repositories. At a 10-minute job timeout and four scheduled runs per day, the worst-case scheduled usage is about 1,200 minutes per 30-day month, before other workflows use the same allowance. To guarantee no overage charges, configure an Actions spending budget that stops usage at $0, or do not enable it on a billable account. When the free allowance is exhausted, runs may stop. This is a no-cost option only within the applicable free allowance and is less reliable than a dedicated scheduler.

Do not start the automation from Gunicorn workers. Without an independent scheduler, recurring emails and abandoned-cart reminders will not run automatically.

The migration only adds new tables/columns and does not erase existing store data. Run it before enabling automation. All automation schedules default to paused. Admin previews render current catalog content; test sends go only to MAIL_TEST_ADDRESS and are limited to three per admin per hour. A sent status means provider acceptance, not confirmed inbox placement.

Only verified, subscribed customer accounts are eligible. Cart reminders only cover authenticated accounts whose cart snapshot is past the configured inactivity window; checkout activity suppresses the reminder for 30 minutes, and an order request resolves the episode. Guest carts are never emailed. Product prices and availability are revalidated before each reminder. One reminder is sent per episode by default, and the frequency limit is at most one marketing message per account per rolling 24 hours.

Current offers require a stored previous price higher than the current price; this schema has no separate offer start/end dates. Weekly ranking uses Completed and Paid orders from the last 90 days, with the existing lifetime recorded cart-add aggregate as a secondary signal because that legacy counter has no event timestamps. If activity is insufficient, the campaign calls items fresh picks instead of claiming popularity. Only published reviews are eligible for highlights.

For Brevo delivery and bounce events, set `BREVO_WEBHOOK_TOKEN` to a long random secret and configure Brevo to POST events to `https://fitystore.top/webhooks/brevo/email-events` with the same value in the `X-Fity-Brevo-Token` header. The endpoint accepts delivered, hard/soft bounce, invalid, spam/complaint, and unsubscribe events, matching messages by Brevo message ID. Hard bounces, complaints, and invalid-address events suppress marketing; soft bounces update delivery status only. Duplicate events do not duplicate consent records. An unset token makes the endpoint reject calls. Verify the provider payload and event names before relying on this feed; admins can still suppress addresses manually.

Brevo needs a verified sender and authenticated domain. Set MAIL_PROVIDER=brevo, BREVO_API_KEY, MAIL_FROM_ADDRESS, and MAIL_FROM_VERIFIED=1 only after sender/domain verification. Set MAIL_TEST_ADDRESS before using the automated test form. Store credentials only in Render environment settings or an untracked local .env file; never put credentials in .env.example.

This feature does not certify legal compliance or inbox delivery. Kenya's Data Protection Act and General Regulations require specific prior consent for direct marketing and a clear, low-effort opt-out. Review the privacy notice, ODPC registration obligations, data retention, and vendor/data-transfer arrangements with qualified counsel before launch.
