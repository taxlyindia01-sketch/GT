# ============================================================
# GoldTrader Pro v4 — FastAPI Backend
# Taxly India Private Limited
# ============================================================

import os
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from pathlib import Path

from database import engine, Base
from routers import (
    auth, invoices, customers, payments,
    cash, advances, stock, reports, admin, export
)
import models  # noqa: F401 — ensures tables are registered


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create all tables if they don't exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(
    title="GoldTrader Pro API",
    description="Complete jewellery business management — GST, TCS, SFT, FIFO",
    version="4.0.0",
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1000)

# ── CORS ─────────────────────────────────────────────────────
# Allow the frontend origin (set FRONTEND_URL env var in production)
FRONTEND_URL = os.getenv("FRONTEND_URL", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL] if FRONTEND_URL != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Routers ───────────────────────────────────────────────
app.include_router(auth.router,      prefix="/api/auth",      tags=["Auth"])
app.include_router(invoices.router,  prefix="/api/invoices",  tags=["Invoices"])
app.include_router(customers.router, prefix="/api/customers", tags=["Customers"])
app.include_router(payments.router,  prefix="/api/payments",  tags=["Payments"])
app.include_router(cash.router,      prefix="/api/cash",      tags=["Cash Register"])
app.include_router(advances.router,  prefix="/api/advances",  tags=["Advances"])
app.include_router(stock.router,     prefix="/api/stock",     tags=["Stock"])
app.include_router(reports.router,   prefix="/api/reports",   tags=["Reports"])
app.include_router(export.router,    prefix="/api/export",    tags=["Export"])
app.include_router(admin.router,     prefix="/api/admin",     tags=["Admin"])


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api")
async def api_root():
    return {"app": "GoldTrader Pro", "version": "4.0.0", "status": "running"}


# ── Serve Frontend (single-file HTML) ────────────────────────
# Serves the HTML file for any non-/api route so the SPA works
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
INDEX_FILE   = FRONTEND_DIR / "index.html"

if INDEX_FILE.exists():
    @app.get("/", include_in_schema=False)
    async def serve_frontend():
        return FileResponse(str(INDEX_FILE))

    @app.get("/{full_path:path}", include_in_schema=False)
    async def catch_all(full_path: str):
        # Don't catch API routes
        if full_path.startswith("api/") or full_path.startswith("docs") or full_path.startswith("openapi"):
            from fastapi import HTTPException
            raise HTTPException(status_code=404)
        return FileResponse(str(INDEX_FILE))
