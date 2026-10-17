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

SQLite local development creates tables through the existing `db.create_all()` startup path. The `migrate-customer-email-schema` command also supports SQLite and creates only the eight customer/account email tables from SQLAlchemy model metadata; for that specific CLI command, app startup skips the broader `ensure_columns()` helper so unrelated order/product columns and tracking tokens are not touched. It verifies required columns, indexes, uniqueness constraints and foreign keys. The SQLite path uses a transaction and reports which tables were created or already existed. An incomplete pre-existing feature table causes a clear error and rollback rather than a destructive repair. A fresh local store must first be initialized with `init-db`; do not run `init-db` against a database with live store records because it can seed starter data.

For an existing PostgreSQL deployment, back up the database and review `migrations/20261017_customer_accounts_email.sql`, then apply the additive migration explicitly:

```powershell
python -m flask --app wsgi:app migrate-customer-email-schema
```

The migration adds customer identity, token, consent, order-link, rate-limit, campaign, and email-message tables. It does not change or delete existing order, product, inventory, payment, image, or admin records. Run it before enabling customer account/email routes on PostgreSQL. The migration is idempotent.

### Email provider setup

No provider or sender is configured by default. Choose an SMTP-capable transactional provider and configure its host, port, TLS mode, username/password, and a sender address that the provider has verified:

```text
MAIL_PROVIDER=smtp
MAIL_FROM_ADDRESS=<provider-verified sender>
MAIL_FROM_NAME=FityStores
MAIL_REPLY_TO=<real inbox the business monitors>
MAIL_SERVER=<provider SMTP host>
MAIL_PORT=587
MAIL_USERNAME=<provider username>
MAIL_PASSWORD=<provider password>
MAIL_USE_TLS=1
MAIL_FROM_VERIFIED=1
MAIL_TEST_ADDRESS=<authorized owner test inbox>
```

Set `MAIL_FROM_VERIFIED=1` only after the provider verifies the sender/domain. `support@fitystore.top`, `orders@fitystore.top`, and `hello@fitystore.top` are naming examples only; this project does not create mailboxes, aliases, or forwarding. A sender identity can send mail but does not itself receive replies. Configure `MAIL_REPLY_TO` to an inbox that exists and is monitored. Buying the domain alone does not include email hosting.

Follow the provider's current DNS instructions to publish SPF and DKIM. Add a DMARC policy for `fitystore.top` and monitor aggregate reports before tightening enforcement. DNS is not changed by this application. Provider plans, free quotas, costs, and setup requirements change; check the selected provider before relying on a free tier.

The admin Customer email page shows whether SMTP is configured, subscriber consent, campaign history, and the last 30 message statuses. SMTP `sent` means the configured relay accepted the message; it does not prove inbox delivery. This SMTP implementation has no delivery/bounce webhook because no provider has been selected. An admin can suppress a permanently bounced address; suppressed accounts are excluded and cannot self-resubscribe. There is no automatic retry worker. Email failures do not roll back committed orders or payment records. Campaign sending is synchronous and intended for low volume; large lists may exceed a web-host request timeout. Do not run a campaign until SMTP is verified with the configured test address.

### Google sign-in setup

Install the pinned dependencies from `requirements.txt`, create an OAuth **Web application** client in Google Cloud, and place its client ID in `GOOGLE_CLIENT_ID`. Add `https://fitystore.top` as an authorized JavaScript origin and these exact HTTPS redirect URIs:

```text
https://fitystore.top/account/google
https://fitystore.top/account/google/link
```

Also authorize `fitystore.top` in the Google consent/branding configuration. The client secret is not used by this Google Identity Services ID-token flow and must not be added to the app. The server verifies the ID token signature, audience, issuer and expiry with `google-auth`, checks `email_verified`, and uses `sub` as the Google identity. Automatic Google registration is limited to Gmail or verified Google Workspace addresses. Existing email accounts are never silently linked; the customer must sign in first and link the matching verified Google address from the account page.

The integration stays unavailable until `GOOGLE_CLIENT_ID` and the `google-auth` package are configured. Google setup/reference: <https://developers.google.com/identity/gsi/web/guides/verify-google-id-token>.

### Safe manual test

1. In a test/local environment, leave real SMTP credentials empty and keep the test database. The app records email as `not_configured` instead of sending it.
2. Register a customer with an unused email and a 12+ character password. Verify the stored password is a hash and the marketing checkbox starts unchecked.
3. In the test database only, inspect the `CustomerToken` hash and use a test-created raw token to POST the verification form. Confirm expiry and single-use behavior.
4. Check that marketing status is `none` when unchecked. If opting in, confirm the separate `marketing_confirm` token only after verifying the account, then unsubscribe using the one-time link.
5. Sign into a verified account before checkout and confirm its order appears on that account only. Submit a guest order and confirm it remains available through the existing WhatsApp/tracking workflow without an account link.
6. To exercise SMTP in tests, mock `app.services.customer_mail._send_smtp`. Never put a live provider credential into test settings.
7. Only after sender verification, configure `MAIL_TEST_ADDRESS`, send one test message from the admin centre, and inspect its status. Preview a campaign and type `SEND` only when the eligible subscriber count and content are correct.

Use HTTPS in production and set `SESSION_COOKIE_SECURE=1`. Registration, login, verification, password reset and Google sign-in are rate-limited per IP/action; session cookies remain HTTP-only and SameSite Lax. Password reset/verification raw tokens are only included in the generated email and are stored in the database as SHA-256 hashes. The email outbox stores recipient, subject, event, status and failure summary, not token links or message bodies. Review privacy wording and any retention schedule with the business owner; this implementation is not a legal compliance certification.
