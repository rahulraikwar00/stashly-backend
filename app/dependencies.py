"""Shared FastAPI dependencies (pull services off `app.state`)."""

from __future__ import annotations

from fastapi import HTTPException, Request

from .config import Settings
from .connectors.base import Connector


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_connector(request: Request) -> Connector:
    connector = request.app.state.connector
    if connector is None:
        raise HTTPException(
            status_code=503,
            detail="No messenger connector configured (set IG_USERNAME/IG_PASSWORD).",
        )
    return connector


def get_mailbox(request: Request):
    """The buffered mailbox store, or None when running on the JSON stores.

    `/messages/links` falls back to a live Instagram fetch when there is no
    mailbox, which keeps local development and the JSON test app working with no
    database. Read with `getattr` so an app that never set it is treated the same
    as one that deliberately set it to None.
    """
    return getattr(request.app.state, "mailbox", None)