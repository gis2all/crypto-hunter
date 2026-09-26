"""SQLite access layer (no ORM: explicit SQL, small tables)."""

from .db import connect, init_db, now_iso  # noqa: F401
