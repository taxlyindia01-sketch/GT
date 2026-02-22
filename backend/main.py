# ============================================================
# GoldTrader Pro v4 — FastAPI Backend
# Taxly India Private Limited
# ============================================================

import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from pathlib import Path

from database import engine, Base
from config import settings
from routers import (
    auth, invoices, customers, payments,
    cash, advances, stock, reports, admin, export
)
import models  # noqa: F401 — registers all ORM tables



# ── Startup diagnostics (always visible in Render logs) ──────
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
_log = logging.getLogger("goldtrader")
_log.info("=" * 50)
_log.info("GoldTrader Pro v4 — Python module loading...")
_log.info(f"Python: {sys.version}")
_log.info("=" * 50)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup: create all DB tables (safe to run on every deploy).
    If DB is unreachable the error is logged clearly before exiting.
    """
    import logging
    logger = logging.getLogger("goldtrader.startup")
    logger.info("GoldTrader Pro v4 starting up...")
    logger.info(f"Database URL scheme: {settings.DATABASE_URL[:30]}...")

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ Database tables created/verified successfully")
    except Exception as exc:
        logger.critical(
            f"\n{'='*60}\n"
            f"FATAL: Cannot connect to database at startup.\n"
            f"Error: {type(exc).__name__}: {exc}\n"
            f"DATABASE_URL (first 40 chars): {settings.DATABASE_URL[:40]}\n"
            f"Make sure DATABASE_URL is set correctly in Render environment.\n"
            f"{'='*60}"
        )
        raise  # Re-raise so uvicorn exits with status 1 and shows the error

    yield  # App is running

    # Shutdown
    await engine.dispose()
    logger.info("GoldTrader Pro shutdown complete.")


app = FastAPI(
    title="GoldTrader Pro API",
    description="Jewellery CRM — GST, TCS, SFT, FIFO",
    version="4.0.0",
    lifespan=lifespan,
)

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


# ── Health & root ─────────────────────────────────────────────
@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok"}


@app.get("/api", tags=["Health"])
async def api_root():
    return {"app": "GoldTrader Pro", "version": "4.0.0", "status": "running"}


# ── Serve Frontend HTML ───────────────────────────────────────
# The frontend/index.html is one directory above backend/
FRONTEND_INDEX = Path(__file__).parent.parent / "frontend" / "index.html"


@app.get("/", include_in_schema=False)
async def serve_root():
    if FRONTEND_INDEX.exists():
        return FileResponse(str(FRONTEND_INDEX))
    return {"message": "GoldTrader Pro API is running. See /docs for API documentation."}


@app.get("/{full_path:path}", include_in_schema=False)
async def catch_all(full_path: str):
    """Serve frontend for all non-API paths (SPA routing)."""
    # Don't intercept API, docs, or openapi routes
    skip = ("api/", "docs", "redoc", "openapi.json")
    if any(full_path.startswith(s) for s in skip):
        raise HTTPException(status_code=404, detail="Not found")
    if FRONTEND_INDEX.exists():
        return FileResponse(str(FRONTEND_INDEX))
    raise HTTPException(status_code=404, detail="Not found")
