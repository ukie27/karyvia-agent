FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates git bubblewrap openssh-client libmagic1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN uv venv --seed "$VIRTUAL_ENV"

# Install Python dependencies first (cached layer).
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
RUN mkdir -p src/karyvia && touch src/karyvia/__init__.py && \
    uv pip install --python "$VIRTUAL_ENV/bin/python" --no-cache . && \
    rm -rf src

# Copy the full source and install
COPY src/ src/
RUN uv pip install --python "$VIRTUAL_ENV/bin/python" --no-cache .

# Official plugins are discovered through entry points, so they must actually be
# installed. Pick what the image should serve; each plugin declares its own
# platform SDK as an optional extra (D32-D34: the host depends on none of them).
ARG KARYVIA_PLUGINS=
COPY plugins/ plugins/
RUN for plugin in $(printf '%s' "$KARYVIA_PLUGINS" | tr ',' ' '); do \
        uv pip install --python "$VIRTUAL_ENV/bin/python" --no-cache \
            "./plugins/karyvia-plugin-${plugin}"; \
    done

# Create the non-root user and hand ownership of the writable virtualenv to it.
RUN useradd -m -u 1000 -s /bin/bash nanobot && \
    mkdir -p /home/nanobot/.karyvia && \
    chown -R nanobot:nanobot /home/nanobot /app/.venv

COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

# Start as root so the entrypoint can fix ownership of a mounted data directory
# before dropping to the non-root nanobot user via setpriv. The entrypoint drops
# privileges on every root start and fails closed if it cannot.
USER root
ENV HOME=/home/nanobot
# Ensure crash output reaches container logs.
ENV PYTHONUNBUFFERED=1 PYTHONFAULTHANDLER=1

# The OpenAI-compatible HTTP plugin listens here when enabled (its DEFAULT_PORT).
EXPOSE 8760

ENTRYPOINT ["entrypoint.sh"]
# `D31` 之后默认跑无头模式：装了 Channel 插件就服务它们，没装就以退出码 1 说明。
CMD ["serve"]
