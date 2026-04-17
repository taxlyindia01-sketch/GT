-- Migration 07: Add round_off column to invoices
-- Run ONCE on your live PostgreSQL database.
-- Safe to run multiple times (IF NOT EXISTS check).
-- Command: psql -U postgres -d postgres -h localhost -f sql/07_add_round_off.sql

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name  = 'invoices'
          AND column_name = 'round_off'
    ) THEN
        ALTER TABLE invoices
            ADD COLUMN round_off NUMERIC(10, 2) NOT NULL DEFAULT 0;
        RAISE NOTICE '✅ Added round_off column to invoices.';
    ELSE
        RAISE NOTICE 'ℹ️  round_off already exists on invoices — skipping.';
    END IF;
END $$;
