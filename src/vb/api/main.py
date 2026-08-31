"""FastAPI application entrypoint. Run: uvicorn vb.api.main:app --reload"""
from __future__ import annotations

from fastapi import FastAPI

from .. import __version__
from .routers import conferences, contests, health, players, teams

app = FastAPI(
    title="VB Data API",
    version=__version__,
    description="NCAA D1 women's volleyball data — teams, rosters, and derived stats.",
)

app.include_router(health.router)
app.include_router(conferences.router)
app.include_router(teams.router)
app.include_router(players.router)
app.include_router(contests.router)


@app.get("/", tags=["health"])
def root() -> dict:
    return {"name": "vb-data", "version": __version__, "docs": "/docs"}
