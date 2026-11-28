import os
from pathlib import Path
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent


def _sqlalchemy_database_uri(database_url: str) -> str:
    if database_url.startswith(("postgresql://", "postgres://")):
        return "postgresql+psycopg://" + database_url.split("://", 1)[1]
    return database_url


class Config:
    APP_ENV = os.environ.get("APP_ENV", "development")
    FLASK_ENV = os.environ.get("FLASK_ENV", "")
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = _sqlalchemy_database_uri(
        os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'instance' / 'fity.sqlite3'}")
    )
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_TIME_LIMIT = None
    MAX_CONTENT_LENGTH = 4 * 1024 * 1024
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", str(BASE_DIR / "app" / "static" / "uploads"))
    PAYMENT_PROOF_FOLDER = os.environ.get("PAYMENT_PROOF_FOLDER", str(BASE_DIR / "instance" / "payment_proofs"))
    ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}
    CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
    CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
    CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0" if os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true"} else "1").lower() in {"1", "true", "yes"}
    PERMANENT_SESSION_LIFETIME = timedelta(days=30)
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    MAIL_PROVIDER = os.environ.get("MAIL_PROVIDER", "")
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
    BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
    BREVO_WEBHOOK_TOKEN = os.environ.get("BREVO_WEBHOOK_TOKEN", "")
    MAIL_FROM_ADDRESS = os.environ.get("MAIL_FROM_ADDRESS", "").strip() or "orders@fitystore.top"
    MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "FityStores")
    MAIL_REPLY_TO = os.environ.get("MAIL_REPLY_TO", "")
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "1").lower() in {"1", "true", "yes"}
    MAIL_USE_SSL = os.environ.get("MAIL_USE_SSL", "0").lower() in {"1", "true", "yes"}
    MAIL_FROM_VERIFIED = os.environ.get("MAIL_FROM_VERIFIED", "0").lower() in {"1", "true", "yes"}
    MAIL_TEST_ADDRESS = os.environ.get("MAIL_TEST_ADDRESS", "")
    SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "oxygenvessels@gmail.com").strip() or "oxygenvessels@gmail.com"
    EMAIL_MARKETING_QUEUE_ENABLED = os.environ.get("EMAIL_MARKETING_QUEUE_ENABLED", "false").strip().lower() == "true"
    EMAIL_MARKETING_PREPARATION_ENABLED = os.environ.get("EMAIL_MARKETING_PREPARATION_ENABLED", "false").strip().lower() == "true"
    EMAIL_MARKETING_AUTO_SCHEDULE_GENERATED_ENABLED = os.environ.get("EMAIL_MARKETING_AUTO_SCHEDULE_GENERATED_ENABLED", "false").strip().lower() == "true"
    EMAIL_MARKETING_NEW_ARRIVAL_LOOKBACK_DAYS = max(1, min(30, int(os.environ.get("EMAIL_MARKETING_NEW_ARRIVAL_LOOKBACK_DAYS", "7"))))
    EMAIL_MARKETING_ABANDONED_CART_IDLE_MINUTES = max(5, min(10080, int(os.environ.get("EMAIL_MARKETING_ABANDONED_CART_IDLE_MINUTES", "180"))))
    EMAIL_MARKETING_NEW_PRODUCTS_ENABLED = os.environ.get("EMAIL_MARKETING_NEW_PRODUCTS_ENABLED", "false").strip().lower() == "true"
    EMAIL_MARKETING_OFFERS_ENABLED = os.environ.get("EMAIL_MARKETING_OFFERS_ENABLED", "false").strip().lower() == "true"
    EMAIL_MARKETING_WEEKLY_PICKS_ENABLED = os.environ.get("EMAIL_MARKETING_WEEKLY_PICKS_ENABLED", "false").strip().lower() == "true"
    EMAIL_MARKETING_ABANDONED_CART_ENABLED = os.environ.get("EMAIL_MARKETING_ABANDONED_CART_ENABLED", "false").strip().lower() == "true"
    EMAIL_MARKETING_SEASONAL_ENABLED = os.environ.get("EMAIL_MARKETING_SEASONAL_ENABLED", "false").strip().lower() == "true"
    EMAIL_MARKETING_CUSTOM_ENABLED = os.environ.get("EMAIL_MARKETING_CUSTOM_ENABLED", "false").strip().lower() == "true"
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://fitystore.top").strip().rstrip("/")
    ANDROID_PACKAGE_ID = os.environ.get("ANDROID_PACKAGE_ID", "top.fitystore.app").strip()
    ANDROID_ASSETLINKS_SHA256_CERT_FINGERPRINTS = os.environ.get(
        "ANDROID_ASSETLINKS_SHA256_CERT_FINGERPRINTS", ""
    )
    # Manual cart reminder idle time for the current testing window.
    CART_REMINDER_IDLE_MINUTES = max(1, int(os.environ.get("CART_REMINDER_IDLE_MINUTES", "5")))

    @classmethod
    def validate_runtime(cls, config):
        """Reject known or missing secrets when running in a production environment."""
        production = (
            str(config.get("APP_ENV", "")).strip().lower() == "production"
            or str(config.get("FLASK_ENV", "")).strip().lower() == "production"
            or str(os.environ.get("RENDER", "")).strip().lower() == "true"
        )
        if not production:
            return
        secret = str(config.get("SECRET_KEY") or "").strip()
        insecure = {"dev-only-change-me", "change-me", "changeme", "secret", "your-secret-key"}
        repeated_pattern = any(
            len(secret) % width == 0 and secret == secret[:width] * (len(secret) // width)
            for width in range(1, min(8, len(secret) // 2) + 1)
        )
        if len(secret) < 32 or secret.lower() in insecure or len(set(secret)) < 12 or repeated_pattern:
            raise RuntimeError(
                "Production requires SECRET_KEY to be explicitly configured with a strong value."
            )

class TestConfig(Config):
    SECRET_KEY = "test-only-explicit-secret-key-not-for-production"
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SERVER_NAME = "example.test"
    SESSION_COOKIE_SECURE = False
    MAIL_PROVIDER = "brevo"
    RESEND_API_KEY = ""
    BREVO_API_KEY = "test-only-brevo-key"
    MAIL_FROM_ADDRESS = "orders@example.test"
    MAIL_FROM_VERIFIED = False

