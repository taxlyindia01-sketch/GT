# config.py — Application settings loaded from environment variables

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── Database (REQUIRED) ───────────────────────────────────
    # Render provides postgresql:// — our database.py auto-converts it
    DATABASE_URL: str = "postgresql+asyncpg://postgres:GT123@localhost:5432/goldtrader"

    # ── Security (REQUIRED in production) ────────────────────
    JWT_SECRET: str = "change-me-generate-with-secrets-token-hex-32"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080  # 7 days

    # ── Taxly Super-Admin ─────────────────────────────────────
    TAXLY_ADMIN_USERNAME: str = "Taxly"
    # Default hash is for password: @Gsf025@
    # Regenerate: python -c "import bcrypt; print(bcrypt.hashpw(b'@Gsf025@', bcrypt.gensalt()).decode())"
    ADMIN_PASSWORD_HASH: str = "$2b$12$ZhJuQ.tZKyxLgVT/GqrzBeX20BFpN0sFKhzMBPUW0HbtnYKR8Mlsi"

    # ── CORS ──────────────────────────────────────────────────
    # Set to your Render service URL or keep * for open CORS
    FRONTEND_URL: str = "*"

    # ── Google OAuth (optional) ───────────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # ── Trial ────────────────────────────────────────────────
    TRIAL_DAYS: int = 10

    # ── Email (optional) ─────────────────────────────────────
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    FROM_EMAIL: str = "GoldTrader Pro <support@goldtraderpro.in>"

    # ── S3 Storage (optional) ────────────────────────────────
    S3_BUCKET: str = ""
    S3_ENDPOINT: str = ""
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""

    # ── Redis (optional) ─────────────────────────────────────
    REDIS_URL: str = ""

    # ── Connection pool ───────────────────────────────────────
    # pool_size: persistent connections kept open per process
    # max_overflow: extra connections allowed above pool_size (burst capacity)
    # pool_timeout: seconds to wait for a free connection before raising
    # pool_recycle: recycle connections older than N seconds (prevents stale TCP)
    DB_POOL_SIZE:     int = 5
    DB_MAX_OVERFLOW:  int = 10
    DB_POOL_TIMEOUT:  int = 30
    DB_POOL_RECYCLE:  int = 300

    # ── Misc ─────────────────────────────────────────────────
    DEBUG: bool = False
    APP_NAME: str = "GoldTrader Pro"

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"  # Don't crash on unknown env vars


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
