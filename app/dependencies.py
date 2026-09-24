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