FROM python:3.12-slim

# uv (dependency & runtime manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# install pinned dependencies first (better layer caching)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# application code
COPY app ./app
COPY seed.py create_user.py ./

ENV RP_DB=/data/roompulse.db \
    UV_FROZEN=1
RUN mkdir -p /data
EXPOSE 8080

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
