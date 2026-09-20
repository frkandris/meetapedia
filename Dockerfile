FROM python:3.12-slim

WORKDIR /app

ENV HOST=0.0.0.0

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY scraper/ ./scraper/
COPY config/   ./config/
# Maintenance scripts (provider scoring, city import) must be runnable in the
# container: that is where the database and the API keys live.
COPY scripts/  ./scripts/
COPY tailwind.config.js .

RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir .

# Build Tailwind CSS (pytailwindcss uses the standalone binary, no Node needed)
RUN pip install --no-cache-dir pytailwindcss && \
    tailwindcss -i ./scraper/web/static/css/input.css \
                -o ./scraper/web/static/css/app.css \
                --minify && \
    pip uninstall -y pytailwindcss

# Embed build timestamp so the version string works without git history
RUN TZ=Europe/Budapest date '+%Y-%m-%d.%H:%M' > /app/VERSION

# data/ and config/ can be mounted as persistent volumes.

EXPOSE 8000

# Liveness, and generous on purpose. Failing this check makes Traefik drop the
# route, so every visitor gets a 404 — the cost of a false negative is a total
# outage, and the cost of a false positive is a few slow minutes. On 2026-08-18
# the pipeline's synchronous writes pushed /healthz to 6s and the old 10s/3
# margin was one bad minute away from killing a perfectly working container.
# Being busy is not being dead.
HEALTHCHECK --interval=30s --timeout=30s --start-period=90s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=25)" || exit 1

CMD ["python", "-m", "scraper.main"]
