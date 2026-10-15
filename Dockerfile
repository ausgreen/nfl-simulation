# The hosted worker (docs/deploy.md): polls ESPN, simulates, publishes to R2. No ports: it only makes outbound requests.
# Python 3.13 matches the local venv, so a simulator set-up cache built on a laptop loads on the server.
FROM python:3.13-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1

# dependencies first, so code-only changes reuse this layer
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra hosted --no-install-project
COPY nflsim ./nflsim
RUN uv sync --frozen --no-dev --extra hosted

# data/ (~230 MB, mostly the weather archive). data/live/ is the Fly volume at run time, so the one file in it that
# has no generator is kept aside and copied onto the volume by the entrypoint.
COPY data ./data
RUN mkdir -p seed && mv data/live/wf_rows.parquet seed/
COPY deploy/entrypoint.sh /entrypoint.sh

ENV NFLSIM_DATA=/app/data PATH=/app/.venv/bin:$PATH
ENTRYPOINT ["/entrypoint.sh"]
CMD ["nflsim", "worker", "--publish", "r2"]
