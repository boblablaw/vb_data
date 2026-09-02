"""FastAPI application entrypoint. Run: uvicorn vb.api.main:app --reload"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .. import __version__
from ..auth.security import hash_password
from ..config import settings
from ..db import SessionLocal
from ..models import User
from .routers import (
    admin,
    ask,
    auth,
    conferences,
    contests,
    favorites,
    games,
    health,
    passkeys,
    players,
    stats,
    teams,
)

log = logging.getLogger("vb.api")


def _init_sentry() -> None:
    """Initialize Sentry error tracking + performance tracing when a DSN is configured.

    No-op when ``SENTRY_DSN`` is blank (local dev / tests), so the app is unchanged without it.
    Must run *before* the FastAPI app is created so Sentry's ASGI middleware wraps every request.
    Privacy: ``send_default_pii=False`` and ``max_request_body_size="never"`` keep passwords, the
    magic-link token, cookies, and emails out of captured events (this is a public repo).
    """
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.sentry_environment,
            release=f"vb-data@{__version__}",
            traces_sample_rate=settings.sentry_traces_sample_rate,
            profiles_sample_rate=settings.sentry_profiles_sample_rate,
            send_default_pii=False,        # no cookies/headers/user email attached
            max_request_body_size="never",  # never capture request bodies (they carry secrets)
        )
        log.info("Sentry initialized (env=%s).", settings.sentry_environment)
    except Exception as e:  # observability must never take down the app
        log.warning("Sentry init skipped: %s", e)


_init_sentry()


def _bootstrap_admin() -> None:
    """Ensure an admin account exists. Creates/promotes ADMIN_EMAIL if none is present.

    The password is only set when creating the row (existing users keep their password). Secrets
    are never logged — only the email and a created/promoted note.
    """
    email = settings.admin_email.lower().strip()
    if not email:
        return
    db = SessionLocal()
    try:
        has_admin = db.scalar(select(User).where(User.is_admin.is_(True)).limit(1))
        if has_admin is not None:
            return
        existing = db.scalar(select(User).where(User.email == email))
        if existing is not None:
            existing.is_admin = True
            existing.email_verified = True
            db.commit()
            log.info("Promoted existing user %s to admin.", email)
            return
        db.add(User(
            email=email,
            password_hash=hash_password(settings.admin_password),
            name="Admin",
            is_admin=True,
            email_verified=True,
        ))
        db.commit()
        log.info("Created bootstrap admin user %s.", email)
    except Exception as e:  # never let bootstrap crash the app (e.g. read-only DB / no table yet)
        db.rollback()
        log.warning("Admin bootstrap skipped: %s", e)
    finally:
        db.close()


# --------------------------------------------------------------------------- MCP (optional)
# The MCP server is mounted defensively: an mcp-SDK API mismatch must not take down the REST API.
_mcp_app = None
_mcp_token_check = None
try:  # pragma: no cover - import guarded so a missing/incompatible SDK degrades gracefully
    from ..mcp.server import streamable_app as _mcp_streamable_app
    from ..mcp.server import token_is_valid as _mcp_token_check

    _mcp_app = _mcp_streamable_app()
except Exception as e:
    log.warning("MCP server not mounted: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap_admin()
    if _mcp_app is not None and getattr(_mcp_app, "router", None) is not None:
        # Drive the MCP session manager's lifespan alongside the app's.
        async with _mcp_app.router.lifespan_context(_mcp_app):
            yield
    else:
        yield


app = FastAPI(
    title="VBallr API",
    version=__version__,
    description="NCAA D1 women's volleyball data — teams, rosters, and derived stats.",
    lifespan=lifespan,
)

# Read (public) routers.
app.include_router(health.router)
app.include_router(conferences.router)
app.include_router(teams.router)
app.include_router(players.router)
app.include_router(contests.router)
app.include_router(games.router)
app.include_router(stats.router)

# Auth + personalization routers.
app.include_router(auth.router)
app.include_router(auth.email_router)
app.include_router(passkeys.router)
app.include_router(favorites.router)
app.include_router(admin.router)
app.include_router(ask.router)


if _mcp_app is not None:
    @app.middleware("http")
    async def _mcp_bearer_gate(request: Request, call_next):
        """Require a valid admin-set bearer token for anything under /mcp."""
        if request.url.path.rstrip("/") == "/mcp" or request.url.path.startswith("/mcp/"):
            auth_header = request.headers.get("authorization", "")
            token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else None
            if not (_mcp_token_check and _mcp_token_check(token)):
                return JSONResponse({"error": "Invalid or missing MCP token."}, status_code=401)
        return await call_next(request)

    app.mount("/mcp", _mcp_app)


_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_HAS_UI = os.path.isdir(_STATIC_DIR)


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    """One structured line per request in the container logs (method path -> status (NNNms)).

    Skips static/asset noise and the frequently-polled health check so `docker logs vb-api` stays
    readable. Complements Sentry (which needs a DSN); this always runs.
    """
    path = request.url.path
    if path.startswith(("/assets/", "/ui/")) or path == "/health":
        return await call_next(request)
    start = time.perf_counter()
    resp = await call_next(request)
    dur_ms = (time.perf_counter() - start) * 1000
    log.info("%s %s -> %d (%.0fms)", request.method, path, resp.status_code, dur_ms)
    return resp


@app.middleware("http")
async def _cache_static_assets(request: Request, call_next):
    """Let browsers serve logos/photos from local cache without re-hitting the server.

    StaticFiles only sends ETag/Last-Modified, so every logo triggers a 304 revalidation round-trip
    per page load. A day-long max-age skips that entirely; logos change rarely, and when they do the
    change propagates within a day (and immediately for first-time/expired caches via the ETag).
    """
    resp = await call_next(request)
    if request.method in ("GET", "HEAD") and "/assets/" in request.url.path and resp.status_code == 200:
        resp.headers.setdefault("Cache-Control", "public, max-age=86400")
    return resp


@app.get("/", include_in_schema=False, response_model=None)
def root() -> RedirectResponse | dict:
    # Send humans to the web UI; fall back to a JSON descriptor when it isn't packaged.
    if _HAS_UI:
        return RedirectResponse(url="/ui/")
    return {"name": "vb-data", "version": __version__, "docs": "/docs"}


def _asset_version() -> str:
    """Short hash of the served assets' mtimes — changes on every deploy (git reset --hard rewrites
    the files), so appending it as ?v= forces browsers to fetch fresh app.js/styles.css instead of
    serving a stale cached copy."""
    h = hashlib.sha1()
    for fn in ("index.html", "app.js", "styles.css"):
        p = os.path.join(_STATIC_DIR, fn)
        if os.path.exists(p):
            h.update(str(os.path.getmtime(p)).encode())
    return h.hexdigest()[:8]


# Serve the UI shell ourselves (before the /ui mount, so it wins for the index) with the asset
# version stamped onto the app.js/styles.css URLs. Sub-resources still come from the mount below.
if _HAS_UI:
    _ASSET_VER = _asset_version()

    @app.get("/ui/", include_in_schema=False)
    @app.get("/ui/index.html", include_in_schema=False)
    def _ui_index() -> HTMLResponse:
        with open(os.path.join(_STATIC_DIR, "index.html"), encoding="utf-8") as f:
            html = f.read()
        html = (html
                .replace('href="styles.css"', f'href="styles.css?v={_ASSET_VER}"')
                .replace('src="app.js"', f'src="app.js?v={_ASSET_VER}"'))
        return HTMLResponse(html)


# Mounted LAST so it never shadows an API route. html=True serves index.html at /ui/.
if _HAS_UI:
    app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")
