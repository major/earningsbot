# syntax=docker/dockerfile:1

# 🏗️ Stage 1: build a self-contained virtual environment with uv.
FROM python:3.15-slim-trixie AS builder

# 📌 Pinned uv version. Keep it in step with the uv_build range in pyproject.toml.
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /bin/

# ⚙️ Use the base image's Python, never a downloaded one. Skip dev dependencies.
# 🔗 Copy files instead of hard links, because the cache and venv are on different mounts.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_NO_DEV=1 \
    UV_NO_EDITABLE=1

WORKDIR /app

# 📦 Install third-party dependencies first. This layer stays cached until the lock changes.
# 🔐 COPY instead of bind mounts, because SELinux blocks bind-mounted user_home_t files here.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project

# 📁 Copy only what the project build needs. pyproject.toml reads README.md as its readme.
COPY README.md ./
COPY src/ ./src/

# 🧱 Install the project itself as a regular (non-editable) package.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked


# 🚀 Stage 2: the small runtime image. It has no uv and no source tree.
# 🧩 Same base image as the builder, so the venv's python symlink still resolves.
FROM python:3.15-slim-trixie

LABEL org.opencontainers.image.title="earningsbot" \
      org.opencontainers.image.description="Polls SEC EDGAR for 8-K earnings filings and posts alerts to Discord."

# 👤 A fixed UID and GID make volume ownership predictable across hosts.
RUN groupadd --gid 10001 earningsbot \
 && useradd --uid 10001 --gid earningsbot \
        --home-dir /home/earningsbot --create-home \
        --shell /usr/sbin/nologin earningsbot \
 && mkdir /data \
 && chown earningsbot:earningsbot /data

# 🗂️ The seen-filings database lives on /data. Secrets are passed at run time, never baked in.
# 🏠 HOME is writable, so edgartools can keep its cache there.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    EARNINGSBOT_STATE_PATH=/data/seen.sqlite3 \
    HOME=/home/earningsbot

COPY --from=builder /app/.venv /app/.venv

WORKDIR /home/earningsbot
VOLUME ["/data"]

USER earningsbot

# 🎯 Run the console script directly. Extra args such as --once or --interval go after the image name.
# 🧹 An empty CMD clears the base image default, so no stray argument is passed.
ENTRYPOINT ["earningsbot"]
CMD []
