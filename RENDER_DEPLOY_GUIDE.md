# GoldTrader Pro v4 — Render.com Deploy Guide

## How it works on Render

One web service (FastAPI) serves **both** the API and the frontend HTML.
One PostgreSQL database.  
Total cost: **Free** (with Render's free tier).

```
Browser → https://goldtrader-backend.onrender.com/
                    │
                    ├── GET /          → serves frontend/index.html
                    ├── GET /api/*     → FastAPI endpoints
                    └── GET /health    → health check
```

---

## Step-by-step deploy

### 1. Push to GitHub

Your repo should look like this:
```
your-repo/
├── backend/          ← Python/FastAPI (this is rootDir)
│   ├── main.py
│   ├── requirements.txt
│   ├── config.py
│   ├── database.py
│   ├── models/
│   ├── routers/
│   └── utils/
├── frontend/
│   └── index.html
├── sql/
│   ├── 01_schema.sql
│   └── 02_views_and_triggers.sql
└── render.yaml
```

### 2. Create PostgreSQL on Render

1. Render dashboard → **New** → **PostgreSQL**
2. Name: `goldtrader-db`
3. Plan: **Free**
4. Click **Create Database**
5. Once created: copy the **Internal Database URL**
   - Looks like: `postgresql://goldtrader:xxxx@dpg-xxxx-a.oregon-postgres.render.com/goldtrader_pro`

### 3. Create the Web Service

1. **New** → **Web Service** → connect your GitHub repo
2. Settings:
   | Field | Value |
   |---|---|
   | Name | `goldtrader-backend` |
   | Root Directory | `backend` |
   | Runtime | Python 3 |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
   | Plan | Free |

### 4. Set Environment Variables

In your web service → **Environment** tab, add these:

| Variable | Value |
|---|---|
| `DATABASE_URL` | *(paste Internal DB URL from Step 2 — our code auto-converts it)* |
| `JWT_SECRET` | *(run: `python -c "import secrets; print(secrets.token_hex(32))"`)* |
| `TAXLY_ADMIN_USERNAME` | `Taxly` |
| `ADMIN_PASSWORD_HASH` | `$2b$12$ZhJuQ.tZKyxLgVT/GqrzBeX20BFpN0sFKhzMBPUW0HbtnYKR8Mlsi` |
| `TRIAL_DAYS` | `10` |
| `DEBUG` | `False` |
| `FRONTEND_URL` | `*` |

> ⚠️ **IMPORTANT**: Set `ADMIN_PASSWORD_HASH` manually in the Render dashboard — do NOT rely on render.yaml for this value because YAML can misparse the `$` signs in bcrypt hashes.

### 5. Deploy

Click **Create Web Service**. Render will:
1. Pull your code from GitHub
2. Run `pip install -r requirements.txt` (~2-3 min)
3. Start `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Auto-create all database tables on first boot (via SQLAlchemy lifespan)

### 6. Test it

```bash
# Health check (should respond immediately)
curl https://goldtrader-backend.onrender.com/health
# → {"status":"ok"}

# Frontend (should show the login page)
open https://goldtrader-backend.onrender.com/
```

### 7. Create first user

Either use the Sign Up tab in the UI, or via API:
```bash
curl -X POST https://goldtrader-backend.onrender.com/api/auth/signup-demo \
  -H "Content-Type: application/json" \
  -d '{"name":"Admin","mobile":"9999999999","company_name":"My Jewellers","password":"Admin@123"}'
```

---

## Troubleshooting

### Build failed: "could not find a version that satisfies the requirement..."
→ Check `requirements.txt` package names are spelled correctly (PyPI names).
→ `weasyprint` has been removed — it requires system libs not available on Render.

### "ModuleNotFoundError" on startup
→ A package is in `imports` but not in `requirements.txt`.
→ Check Render logs: **Service → Logs**.

### "connection refused" / DB errors on startup
→ `DATABASE_URL` env var not set, or wrong format.
→ Render's Internal URL is `postgresql://...` — our `database.py` converts it automatically.
→ DO NOT use the External URL for the app (use Internal URL only).

### "Invalid credentials" for Taxly admin login
→ `ADMIN_PASSWORD_HASH` env var not set in Render dashboard.
→ Set it manually: `$2b$12$ZhJuQ.tZKyxLgVT/GqrzBeX20BFpN0sFKhzMBPUW0HbtnYKR8Mlsi`
→ This is the bcrypt hash of `@Gsf025@`.

### Render free tier: app sleeps after 15 min
→ Free web services spin down after 15 minutes of inactivity.
→ First request after sleep takes ~30 seconds to wake up.
→ Upgrade to **Starter ($7/mo)** to keep it always on.

### PostgreSQL free tier expires in 90 days
→ Render deletes free PostgreSQL databases after 90 days.
→ Upgrade to **Starter ($7/mo)** before the 90-day mark.

---

## Local development

```bash
cd backend

# Create virtual env
python -m venv venv && source venv/bin/activate

# Install deps
pip install -r requirements.txt

# Set up local DB
createdb goldtrader_pro
psql goldtrader_pro < ../sql/01_schema.sql
psql goldtrader_pro < ../sql/02_views_and_triggers.sql

# Configure env
cp .env.example .env
# Edit .env: set DATABASE_URL and JWT_SECRET

# Run
uvicorn main:app --reload --port 8000
# → open http://localhost:8000
```

**Demo login credentials** (if you ran `03_seed_data.sql`):
- Username: `rajesh_admin` / Password: `admin123`
- Taxly Admin: `Taxly` / Password: `@Gsf025@`
