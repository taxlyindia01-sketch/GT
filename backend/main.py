# ============================================================
# GoldTrader Pro v4 — FastAPI Backend
# Taxly India Private Limited
# ============================================================

import os
import logging
import sys
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from contextlib import asynccontextmanager
from pathlib import Path
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from database import engine, Base
from config import settings
from limiter import limiter   # shared rate-limiter instance
from routers import (
    auth, invoices, customers, payments,
    cash, advances, stock, reports, admin, export, company
)
import models  # noqa: F401 — registers all ORM tables


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
_log = logging.getLogger("goldtrader")
_log.info("=" * 50)
_log.info("GoldTrader Pro v4 — starting up")
_log.info(f"Python: {sys.version}")
_log.info("=" * 50)


# ── Schema migration helper ───────────────────────────────────
# Adds columns that were introduced in P3 to existing databases.
# Uses IF NOT EXISTS — completely safe to run on every deploy.
_P3_MIGRATIONS = """
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS pan               VARCHAR(10);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS qr_code_url       TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS upi_id            VARCHAR(100);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bank_name         VARCHAR(100);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bank_account      VARCHAR(30);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bank_ifsc         VARCHAR(20);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bank_branch       VARCHAR(100);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS authorised_person VARCHAR(200);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS terms_conditions  TEXT;
"""


async def _run_migrations(conn):
    """Execute incremental ALTER TABLE migrations idempotently."""
    logger = logging.getLogger("goldtrader.migrations")
    try:
        for stmt in _P3_MIGRATIONS.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await conn.execute(stmt)
        logger.info("✅ P3 schema migrations applied (IF NOT EXISTS — safe to re-run)")
    except Exception as exc:
        # Log but don't crash — columns may already exist or DB may be read-only
        logger.warning(f"Migration warning (non-fatal): {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = logging.getLogger("goldtrader.startup")
    logger.info("GoldTrader Pro v4 starting up...")
    logger.info(f"Database URL scheme: {settings.DATABASE_URL[:30]}...")

    try:
        async with engine.begin() as conn:
            # Step 1: Create any missing tables (new tables from ORM models)
            await conn.run_sync(Base.metadata.create_all)
            logger.info("✅ Database tables created/verified")

            # Step 2: Apply incremental column migrations for existing tables
            from sqlalchemy import text
            for stmt in _P3_MIGRATIONS.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    try:
                        await conn.execute(text(stmt))
                    except Exception as col_exc:
                        logger.warning(f"Migration stmt skipped: {col_exc}")

            logger.info("✅ Schema migrations complete")

    except Exception as exc:
        logger.critical(
            f"\n{'='*60}\n"
            f"FATAL: Cannot connect to database at startup.\n"
            f"Error: {type(exc).__name__}: {exc}\n"
            f"DATABASE_URL (first 40 chars): {settings.DATABASE_URL[:40]}\n"
            f"Make sure DATABASE_URL is set correctly in Render environment.\n"
            f"{'='*60}"
        )
        raise

    yield

    await engine.dispose()
    logger.info("GoldTrader Pro shutdown complete.")


app = FastAPI(
    title="GoldTrader Pro API",
    description="Jewellery CRM — GST, TCS, SFT, FIFO",
    version="4.2.0",
    lifespan=lifespan,
)

# Rate limiter (shared instance from limiter.py)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(GZipMiddleware, minimum_size=1000)

# ── CORS ──────────────────────────────────────────────────────
FRONTEND_URL = os.getenv("FRONTEND_URL", "*")
origins = ["*"] if FRONTEND_URL == "*" else [FRONTEND_URL]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Routers ───────────────────────────────────────────────
app.include_router(auth.router,      prefix="/api/auth",      tags=["Auth"])
app.include_router(invoices.router,  prefix="/api/invoices",  tags=["Invoices"])
app.include_router(customers.router, prefix="/api/customers", tags=["Customers"])
app.include_router(payments.router,  prefix="/api/payments",  tags=["Payments"])
app.include_router(cash.router,      prefix="/api/cash",      tags=["Cash"])
app.include_router(advances.router,  prefix="/api/advances",  tags=["Advances"])
app.include_router(stock.router,     prefix="/api/stock",     tags=["Stock"])
app.include_router(reports.router,   prefix="/api/reports",   tags=["Reports"])
app.include_router(export.router,    prefix="/api/export",    tags=["Export"])
app.include_router(admin.router,     prefix="/api/admin",     tags=["Admin"])
app.include_router(company.router,   prefix="/api/company",   tags=["Company"])


# ── Health ────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok"}


@app.get("/api", tags=["Health"])
async def api_root():
    return {"app": "GoldTrader Pro", "version": "4.2.0", "status": "running"}


# ── Serve Frontend ────────────────────────────────────────────
FRONTEND_INDEX = Path(__file__).parent.parent / "frontend" / "index.html"


@app.get("/", include_in_schema=False)
async def serve_root():
    if FRONTEND_INDEX.exists():
        return FileResponse(str(FRONTEND_INDEX))
    return {"message": "GoldTrader Pro API is running. See /docs for API documentation."}


@app.get("/{full_path:path}", include_in_schema=False)
async def catch_all(full_path: str):
    skip = ("api/", "docs", "redoc", "openapi.json")
    if any(full_path.startswith(s) for s in skip):
        raise HTTPException(status_code=404, detail="Not found")
    if FRONTEND_INDEX.exists():
        return FileResponse(str(FRONTEND_INDEX))
    raise HTTPException(status_code=404, detail="Not found")
