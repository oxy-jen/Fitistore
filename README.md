# FITY Flask Shop

FITY is a mobile-first Flask e-commerce starter for a small Kenyan merchandise shop. It uses server-rendered pages, SQLite, local image uploads, owner-managed prices and stock, and WhatsApp as the customer enquiry channel.

The app is intentionally honest about manual selling: a WhatsApp message is an enquiry, not proof of payment, delivery, or confirmed stock.

## Architecture

```text
FITY/
  app/
    __init__.py              Application factory, CLI setup, error handlers
    extensions.py            SQLAlchemy, LoginManager, CSRF extension objects
    models.py                Database tables and relationships
    public/routes.py         Home, shop, product detail, cart, checkout, reviews, SEO files
    admin/routes.py          Login, dashboard, products, inventory, orders, reviews, settings
    services/                Cart validation, order creation, inventory, formatting, WhatsApp links
    templates/               Jinja2 pages and partials
    static/css/style.css     FITY design system
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
- `Category`: owner-managed catalogue groups seeded with FITY categories.
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

Uploaded images are stored in `app/static/uploads` for local development. This is free and simple, but many free hosting services erase local files on redeploy or restart. For production, use a host with persistent disk storage or move uploads to an object storage service. Do not upload private address information or payment screenshots.

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
