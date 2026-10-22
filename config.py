import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'instance' / 'fity.sqlite3'}")
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
    MAIL_PROVIDER = os.environ.get("MAIL_PROVIDER", "")
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
    BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
    MAIL_FROM_ADDRESS = os.environ.get("MAIL_FROM_ADDRESS", "")
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
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://fitystore.top").strip().rstrip("/")

class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SERVER_NAME = "example.test"
    SESSION_COOKIE_SECURE = False

