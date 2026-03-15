# ============================================================
# GoldTrader Pro v4 — FastAPI Backend
# Taxly India Private Limited
# ============================================================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path
import os

from database import engine, Base
from routers import (
    auth, invoices, customers, payments,
    cash, advances, stock, reports, admin, export,
    suppliers,
)
import models  # noqa: F401 — ensures tables are registered

# Path to frontend index.html
# Render rootDir is "backend", so frontend is one level up
BASE_DIR     = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"
INDEX_HTML   = FRONTEND_DIR / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(
    title="GoldTrader Pro API",
    description="Complete jewellery business management — GST, TCS, SFT, FIFO",
    version="4.1.0",
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Routers ───────────────────────────────────────────────
app.include_router(auth.router,       prefix="/api/auth",       tags=["Auth"])
app.include_router(invoices.router,   prefix="/api/invoices",   tags=["Invoices"])
app.include_router(customers.router,  prefix="/api/customers",  tags=["Customers"])
app.include_router(payments.router,   prefix="/api/payments",   tags=["Payments"])
app.include_router(cash.router,       prefix="/api/cash",       tags=["Cash Register"])
app.include_router(advances.router,   prefix="/api/advances",   tags=["Advances"])
app.include_router(stock.router,      prefix="/api/stock",      tags=["Stock"])
app.include_router(reports.router,    prefix="/api/reports",    tags=["Reports"])
app.include_router(export.router,     prefix="/api/export",     tags=["Export"])
app.include_router(admin.router,      prefix="/api/admin",      tags=["Admin"])
app.include_router(suppliers.router,  prefix="/api/suppliers",  tags=["Suppliers"])


# ── Frontend: serve index.html for all non-API routes ─────────
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/", response_class=FileResponse)
async def serve_root():
    """Serve the frontend SPA."""
    return FileResponse(str(INDEX_HTML), media_type="text/html")

@app.get("/{full_path:path}", response_class=FileResponse)
async def serve_frontend(full_path: str):
    """Catch-all: serve index.html for any non-API path (SPA routing)."""
    return FileResponse(str(INDEX_HTML), media_type="text/html")
