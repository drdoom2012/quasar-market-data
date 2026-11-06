# Collector, API and symbol overview images (S21). Build with `make images`; targets `collector`, `api` and `overview`,
# platform linux/amd64. The overview is the standard-library tool tools/symbol_overview: no venv, no volume, no config.
# Base images are pinned by digest (multi-arch index), so the push job in images.yml rebuilds on the bases the test
# job used; update tag and digest together.
# The ta-lib 0.8.2 manylinux x86_64 wheel bundles the TA-Lib C library, so no compiler is needed.

FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.8@sha256:3b7b60a81d3c57ef471703e5c83fd4aaa33abcd403596fb22ab07db85ae91347 /uv /usr/local/bin/uv
# Build-time settings only; the runtime stages below do not inherit them.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS overview
RUN groupadd --gid 10001 quasar \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin quasar
WORKDIR /app
# Only the tool (root-owned, readable); it imports nothing outside the standard library.
COPY tools/__init__.py /app/tools/__init__.py
COPY tools/symbol_overview/ /app/tools/symbol_overview/
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "tools.symbol_overview"]
CMD ["--host", "0.0.0.0", "--port", "8765", "--api-url", "http://172.31.250.10:8000"]

FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS runtime
RUN groupadd --gid 10001 quasar \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin quasar \
    && mkdir -p /app/config /app/data \
    && chown 10001:10001 /app/config /app/data
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /app
USER 10001:10001

FROM runtime AS collector
ENTRYPOINT ["quasar-collector"]
CMD ["run", "/app/config/quasar.compose.toml"]

FROM runtime AS api
ENTRYPOINT ["quasar-api"]
CMD ["serve", "/app/config/quasar.compose.toml"]
