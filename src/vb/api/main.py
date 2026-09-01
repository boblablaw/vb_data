"""FastAPI application entrypoint. Run: uvicorn vb.api.main:app --reload"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from .routers import conferences, contests, health, players, stats, teams

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
app.include_router(stats.router)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_HAS_UI = os.path.isdir(_STATIC_DIR)


@app.get("/", include_in_schema=False, response_model=None)
def root() -> RedirectResponse | dict:
    # Send humans to the fantasy UI; fall back to a JSON descriptor when it isn't packaged.
    if _HAS_UI:
        return RedirectResponse(url="/ui/")
    return {"name": "vb-data", "version": __version__, "docs": "/docs"}


# Mounted LAST so it never shadows an API route. html=True serves index.html at /ui/.
if _HAS_UI:
    app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")
