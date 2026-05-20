from __future__ import annotations
import os, sys, importlib.util
from sqlalchemy import create_engine, text

def ensure_sslmode(url: str) -> str:
    if "neon.tech" in url and "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        return url + f"{sep}sslmode=require"
    return url

def normalize_url(raw: str) -> str:
    url = raw.strip()
    if "://" not in url:
        raise ValueError("DATABASE_URL is not a valid URL")
    if "+psycopg" in url or "+psycopg2" in url:
        return url
    has_psycopg  = importlib.util.find_spec("psycopg") is not None
    has_psycopg2 = importlib.util.find_spec("psycopg2") is not None
    if url.startswith("postgresql://"):
        if has_psycopg:
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        elif has_psycopg2:
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        else:
            raise RuntimeError("Install 'psycopg[binary]' or 'psycopg2-binary'.")
    return url

def main():
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)
    url = ensure_sslmode(normalize_url(raw))
    engine = create_engine(url, pool_pre_ping=True, future=True)

    ddl = text("""
        ALTER TABLE yard_transactions
          ADD COLUMN IF NOT EXISTS event_time TEXT
    """)
    backfill = text("""
        UPDATE yard_transactions
        SET event_time = COALESCE(
            NULLIF( (after_snapshot::jsonb ->> 'added_at'), '' ),
            NULLIF( (after_snapshot::jsonb ->> 'created_at'), '' ),
            event_time,
            timestamp
        )
        WHERE event_time IS NULL OR event_time = ''
    """)
    idx = text("""
        CREATE INDEX IF NOT EXISTS idx_tx_event_time
          ON yard_transactions (event_time)
    """)

    with engine.begin() as con:
        con.execute(ddl)
        con.execute(backfill)
        con.execute(idx)

    print("✅ Migration completed successfully.")

if __name__ == "__main__":
    main()