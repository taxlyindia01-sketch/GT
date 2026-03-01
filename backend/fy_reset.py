"""
fy_reset.py — Financial Year Reset Job
=======================================
Resets cash_receipts_fy and sft_flagged for all customers at the start of each FY.

Indian Financial Year: April 1 → March 31.

USAGE
-----
Run as a Render Cron Job (Settings → Cron Jobs):
  Schedule: 0 0 1 4 *   (midnight on April 1 every year — IST = UTC+5:30,
                          so use "30 18 31 3 *" to fire at 00:00 IST on Apr 1)
  Command:  python fy_reset.py

Or invoke manually when needed:
  python fy_reset.py --dry-run       # shows what would be reset, no DB changes
  python fy_reset.py --force         # runs even if today is not April 1

The script is idempotent. Running it twice in the same FY is harmless because
it only zeros values that are non-zero (and logs how many rows it updated).

DATABASE_URL must be set in the environment (same as the main app).
"""

import asyncio
import argparse
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

# ── Config ────────────────────────────────────────────────────
import os

DATABASE_URL: str = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL environment variable is not set.", file=sys.stderr)
    sys.exit(1)

# Render / Heroku supply postgres:// — asyncpg needs postgresql+asyncpg://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

IST = ZoneInfo("Asia/Kolkata")

# ── Engine ────────────────────────────────────────────────────
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ── Reset logic ───────────────────────────────────────────────

async def get_row_count(session: AsyncSession) -> tuple[int, int]:
    """Return (total_customers, flagged_customers) before reset."""
    result = await session.execute(
        text("SELECT COUNT(*), SUM(CASE WHEN sft_flagged THEN 1 ELSE 0 END) FROM customers")
    )
    row = result.one()
    return int(row[0] or 0), int(row[1] or 0)


async def run_reset(dry_run: bool = False) -> None:
    now_ist = datetime.now(IST)
    today   = now_ist.date()

    print(f"[{now_ist.isoformat()}] GoldTrader Pro — FY Reset Job")
    print(f"  Today (IST):  {today}")
    print(f"  Dry run:      {dry_run}")
    print()

    async with AsyncSessionLocal() as session:
        total, flagged = await get_row_count(session)
        print(f"  Customers total:   {total}")
        print(f"  SFT-flagged:       {flagged}")
        print(f"  Non-zero FY cash:  (counting…)")

        nonzero_result = await session.execute(
            text("SELECT COUNT(*) FROM customers WHERE cash_receipts_fy != 0")
        )
        nonzero = int(nonzero_result.scalar() or 0)
        print(f"                     {nonzero}")
        print()

        if dry_run:
            print("  DRY RUN — no changes written to database.")
            print(f"  Would reset: {nonzero} rows (cash_receipts_fy → 0)")
            print(f"  Would clear: {flagged} SFT flags")
            return

        # Perform the reset — single UPDATE, no Python loop needed
        result = await session.execute(
            text("""
                UPDATE customers
                SET
                    cash_receipts_fy = 0,
                    sft_flagged      = FALSE
                WHERE cash_receipts_fy != 0
                   OR sft_flagged = TRUE
            """)
        )
        rows_updated = result.rowcount

        # Write an audit record to a dedicated table if it exists, otherwise log to stdout
        try:
            await session.execute(
                text("""
                    INSERT INTO fy_reset_log (reset_date, rows_updated, triggered_by)
                    VALUES (:d, :r, :t)
                """),
                {"d": today, "r": rows_updated, "t": "fy_reset.py cron"}
            )
        except Exception:
            pass  # table may not exist yet — that's OK, stdout log is the audit trail

        await session.commit()

        print(f"  ✅  Reset complete.")
        print(f"  Rows updated:   {rows_updated}")
        print(f"  Committed at:   {datetime.now(timezone.utc).isoformat()}Z")


def is_april_first() -> bool:
    """Check if today is April 1 in IST."""
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    return now_ist.month == 4 and now_ist.day == 1


# ── Main ──────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="GoldTrader Pro — Financial Year Reset")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be reset without changing the DB")
    parser.add_argument("--force", action="store_true",
                        help="Run even if today is not April 1")
    args = parser.parse_args()

    if not args.dry_run and not args.force and not is_april_first():
        print(f"Today is not April 1 (IST). Use --force to run anyway or --dry-run to preview.")
        print("Exiting without changes.")
        sys.exit(0)

    asyncio.run(run_reset(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
