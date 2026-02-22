# GoldTrader Pro v4 — Render.com Deployment Guide

## Architecture on Render

```
Render PostgreSQL (Free)
        │
        ▼
Render Web Service (Python/FastAPI)  ◄── serves both API + Frontend HTML
        │
        └── GET / → serves frontend/index.html
        └── GET /api/* → FastAPI endpoints
```

> **Single service deployment**: The backend serves the frontend HTML directly.
> No separate static site needed. One URL for everything.

---

## Step-by-Step Deploy

### Step 1: Prepare Your GitHub Repo

Push this entire folder to a GitHub repository:

```
your-repo/
├── backend/          ← FastAPI Python code
├── frontend/         ← index.html (single file frontend)
├── sql/              ← PostgreSQL schema & seed data
└── render.yaml       ← Render Blueprint (optional)
```

### Step 2: Create PostgreSQL Database on Render

1. Go to [render.com](https://render.com) → **New** → **PostgreSQL**
2. Settings:
   - **Name**: `goldtrader-db`
   - **Database**: `goldtrader_pro`
   - **User**: `goldtrader`
   - **Plan**: Free
3. Click **Create Database**
4. **Copy the Internal Database URL** — you'll need it in Step 4

### Step 3: Create the Web Service

1. **New** → **Web Service**
2. Connect your GitHub repo
3. Settings:
   - **Name**: `goldtrader-backend`
   - **Root Directory**: `backend`
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Plan**: Free

### Step 4: Set Environment Variables

In the web service → **Environment** tab, add:

| Variable | Value | Notes |
|---|---|---|
| `DATABASE_URL` | *(paste Internal DB URL from Step 2)* | Render gives `postgresql://...` — our code auto-converts it |
| `JWT_SECRET` | *(generate: `python -c "import secrets; print(secrets.token_hex(32))"`)* | Must be 32+ chars |
| `TAXLY_ADMIN_USERNAME` | `Taxly` | Super-admin username |
| `ADMIN_PASSWORD_HASH` | `$2b$12$ZhJuQ.tZKyxLgVT/GqrzBeX20BFpN0sFKhzMBPUW0HbtnYKR8Mlsi` | Hash of `@Gsf025@` |
| `TRIAL_DAYS` | `10` | |
| `FRONTEND_URL` | `*` | Or your service URL for tighter CORS |
| `DEBUG` | `False` | |

**Optional (leave blank to skip):**
| `GOOGLE_CLIENT_ID` | your Google OAuth client ID |
| `SMTP_USER` | Gmail address for notifications |
| `SMTP_PASSWORD` | Gmail App Password |

### Step 5: Initialize the Database

After first deploy, run the schema SQL on your Render PostgreSQL:

**Option A — Render Shell (easiest)**
1. Go to your Web Service → **Shell**
2. Run: `python -c "from database import engine, Base; import asyncio; asyncio.run(engine.begin().__aenter__())"` 
   *(tables auto-create on first startup via lifespan)*

**Option B — psql from your machine**
```bash
# Get the External Database URL from Render PostgreSQL → Connect
psql "postgresql://goldtrader:PASSWORD@dpg-xxx.oregon-postgres.render.com/goldtrader_pro" \
  -f sql/01_schema.sql \
  -f sql/02_views_and_triggers.sql

# Optional: load sample data (dev only)
# psql "..." -f sql/03_seed_data.sql
```

**Option C — Render → PostgreSQL → Query tab**
Paste and run the contents of `sql/01_schema.sql` and `sql/02_views_and_triggers.sql`.

> **Note**: Tables also auto-create on startup via SQLAlchemy's `Base.metadata.create_all`.
> For the first deploy, this is sufficient — you don't need to run the SQL files manually.

### Step 6: Create the First Admin User

After deploy, use the Render Shell or a POST request:

```bash
curl -X POST https://your-service.onrender.com/api/auth/signup-demo \
  -H "Content-Type: application/json" \
  -d '{"name":"Admin","mobile":"9999999999","company_name":"My Jewellers","password":"yourpassword"}'
```

Or use the Sign Up tab in the frontend UI.

---

## Testing Your Deployment

```bash
# Health check
curl https://your-service.onrender.com/health
# → {"status": "ok"}

# API root
curl https://your-service.onrender.com/api
# → {"app": "GoldTrader Pro", "version": "4.0.0"}

# Frontend
open https://your-service.onrender.com/
# → GoldTrader Pro login page
```

---

## Local Development

```bash
cd backend

# 1. Create virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up local PostgreSQL
createdb goldtrader_pro
psql goldtrader_pro -f ../sql/01_schema.sql
psql goldtrader_pro -f ../sql/02_views_and_triggers.sql
psql goldtrader_pro -f ../sql/03_seed_data.sql   # sample data

# 4. Create .env
cp .env.example .env
# Edit .env: set DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5432/goldtrader_pro

# 5. Run the server
uvicorn main:app --reload --port 8000

# 6. Open frontend
open http://localhost:8000

# Demo credentials (if seed data loaded):
# Username: rajesh_admin   Password: admin123
# Admin: Taxly             Password: @Gsf025@
```

---

## Troubleshooting

### "Application failed to respond" on Render
- Check logs in Render Dashboard → your service → Logs
- Common cause: `DATABASE_URL` not set, or wrong format

### Frontend shows blank/errors in console
- Open browser DevTools → Console
- If you see CORS errors: check `FRONTEND_URL` env var
- If you see connection refused: the API URL detection failed — check `window.location.hostname`

### "Invalid credentials" for admin login
- The `ADMIN_PASSWORD_HASH` must match `@Gsf025@`
- Regenerate: `python -c "import bcrypt; print(bcrypt.hashpw(b'@Gsf025@', bcrypt.gensalt()).decode())"`

### PostgreSQL connection errors
- Render's Internal URL starts with `postgresql://` — our code converts it to `postgresql+asyncpg://`
- External URL (for psql from your machine) starts with `postgresql://` too — use as-is for psql

### Render free tier cold starts
- Free services sleep after 15 minutes of inactivity
- First request after sleep takes ~30 seconds
- Upgrade to Starter ($7/mo) to avoid this

---

## Render Free Tier Limits

| Resource | Free Limit | Notes |
|---|---|---|
| Web Service | 750 hours/month | Enough for 1 service running all month |
| PostgreSQL | 1 GB storage, 90 days | Upgrade before 90 days or data is deleted |
| Bandwidth | 100 GB/month | |
| Build minutes | 400/month | |

---

## Production Checklist

- [ ] `JWT_SECRET` is a random 32+ char string
- [ ] `DEBUG=False`
- [ ] `ADMIN_PASSWORD_HASH` is set (not the default)
- [ ] PostgreSQL plan upgraded from free (90-day limit)
- [ ] HTTPS is enabled (automatic on Render)
- [ ] Google OAuth credentials set if using Google login
