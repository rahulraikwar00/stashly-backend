"""Liveness, plus a real check that Postgres is reachable *and* migrated.

The database check is not incidental: Neon suspends its compute after 5 idle
minutes and this endpoint is what the UptimeRobot keep-alive hits every 5.
Touching the database here keeps the two sides awake together, so the first
real user request doesn't pay a ~500 ms resume penalty.

It also verifies the schema exists. `SELECT 1` succeeds against any Postgres,
including an empty one, so a plain liveness check once reported "database: ok"
while link codes and cursors were being written nowhere at all.

Deliberately still returns HTTP 200 when degraded: Render restarts an instance
after 60s of failing health checks, and a restart means a fresh Instagram login
— so a transient database blip must not be able to trigger a restart loop.
Pass `?strict=1` for a hard 503 gate (CI smoke tests, monitoring that wants it).
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from ..db.pool import db_status

router = APIRouter(tags=["health"])
log = logging.getLogger("health")


@router.get("/health")
def health(request: Request, strict: bool = Query(False)) -> dict:
    settings = request.app.state.settings
    if not settings.postgres_configured:
        return {"status": "ok", "database": "not-configured", "schema": "not-configured"}

    state = db_status(
        settings.database_url,
        drop_channel_binding=settings.drop_channel_binding,
    )
    healthy = state["reachable"] and state["schema"] == "ok"

    body = {
        "status": "ok" if healthy else "degraded",
        "database": "ok" if state["reachable"] else "unreachable",
        "schema": state["schema"],
    }
    if state["missing"]:
        body["missing_tables"] = state["missing"]
        log.error(
            "database is reachable but the schema is incomplete: missing %s — "
            "link codes, cursors and the mailbox are NOT being persisted",
            ", ".join(state["missing"]),
        )

    if strict and not healthy:
        raise HTTPException(status_code=503, detail=body)
    return body