# config.py — All environment-based settings

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── Database ─────────────────────────────────────────────
    # On Render: use the Internal Database URL from your PostgreSQL service
    DATABASE_URL: str = "postgresql+asyncpg://postgres:password@localhost:5432/goldtrader_pro"

    # ── Security ─────────────────────────────────────────────
    JWT_SECRET: str = "change-me-to-a-32-char-random-string!!"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7   # 7 days

    # Taxly super-admin
    # Generate: python -c "import bcrypt; print(bcrypt.hashpw(b'@Gsf025@', bcrypt.gensalt()).decode())"
    ADMIN_PASSWORD_HASH: str = "$2b$12$ZhJuQ.tZKyxLgVT/GqrzBeX20BFpN0sFKhzMBPUW0HbtnYKR8Mlsi"
    TAXLY_ADMIN_USERNAME: str = "Taxly"

    # ── Google OAuth ─────────────────────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # ── Trial settings ────────────────────────────────────────
    TRIAL_DAYS: int = 10

    # ── Frontend URL (for CORS) ───────────────────────────────
    # Set to your Render frontend URL in production, e.g.:
    # https://goldtrader-frontend.onrender.com
    FRONTEND_URL: str = "*"

    # ── Redis (optional — sessions + caching) ────────────────
    REDIS_URL: str = ""

    # ── Email (SMTP) ─────────────────────────────────────────
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    FROM_EMAIL: str = "GoldTrader Pro <support@goldtraderpro.in>"

    # ── S3 (optional — logos & backups) ──────────────────────
    S3_BUCKET: str = ""
    S3_ENDPOINT: str = ""
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""

    # ── Misc ─────────────────────────────────────────────────
    DEBUG: bool = False
    APP_NAME: str = "GoldTrader Pro"

    class Config:
        env_file = ".env"
        case_sensitive = True
        # Don't crash if optional vars are missing
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
