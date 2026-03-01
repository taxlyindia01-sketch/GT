# limiter.py — Shared slowapi rate limiter instance
# 
# Import this module in both main.py (to register on app.state)
# and in any router that uses @limiter.limit decorators.
# Using a SINGLE shared instance avoids the dual-limiter 500 error
# that occurs when routers create their own Limiter instances.

from slowapi import Limiter
from slowapi.util import get_remote_address
from config import settings

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.REDIS_URL or "memory://",
    default_limits=[],   # no global default — set per-endpoint only
)
