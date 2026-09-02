# API-only image for the public fantasy UI + read endpoints. Deliberately NOT a scraper image:
# no Playwright browsers are installed (the `playwright` Python package comes in as a dependency
# but its Chromium download is skipped), so this stays slim. The scrapers keep running from the
# host venv via the systemd timers; this container only serves FastAPI.
#
# Migrations are applied by scripts/deploy.sh on the host (alembic upgrade head) before this
# container starts — the image does not run alembic.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /app

# Install the package (and its deps) from the wheel build. src-layout: setuptools picks up
# `vb` under src/, and package-data ships src/vb/api/static/ into site-packages.
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# Drop privileges — the API needs no write access to anything.
RUN useradd --system --uid 10001 vbapi
USER vbapi

EXPOSE 8091

# Never --reload in a container. Single worker on purpose: WebAuthn/passkey challenges are held in
# per-process in-memory dicts (see vb.api.routers.passkeys), so >1 worker splits login/start and
# login/finish across processes and breaks sign-in with "No pending authentication". One worker is
# ample for this low-traffic, read-only API co-located with PG.
CMD ["uvicorn", "vb.api.main:app", "--host", "0.0.0.0", "--port", "8091", "--workers", "1"]
