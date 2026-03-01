# limiter.py — Shared slowapi rate-limiter instance
#
# A single shared instance is REQUIRED. If each router creates its own
# Limiter(), slowapi hits a storage-state conflict → HTTP 500 on rate-limited routes.
# Both main.py (app.state.limiter) and routers that use @limiter.limit()
# must import this same object.

from slowapi import Limiter
from slowapi.util import get_remote_address
from config import settings

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.REDIS_URL or "memory://",
    default_limits=[],
)
