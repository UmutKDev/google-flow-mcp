# Google Flow MCP server: Streamable HTTP on :8080/mcp, real Google Chrome under Xvfb.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

ARG TARGETARCH
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    DISPLAY=:99 \
    HOME=/home/flow \
    FLOW_OUTPUT_DIR=/tmp/google-flow-mcp \
    PORT=8080

# Xvfb gives Chrome a real (virtual) display: Google's reCAPTCHA scores
# --headless Chrome as a bot, while headed Chrome on Xvfb presents clean.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb xauth ca-certificates curl gnupg fonts-liberation fonts-noto-color-emoji \
        fonts-dejavu-core tini \
    && if [ "${TARGETARCH:-amd64}" = "amd64" ]; then \
        curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google.gpg \
        && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
           > /etc/apt/sources.list.d/google-chrome.list \
        && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable ; \
       fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . \
    # arm64 has no Google Chrome build: fall back to Playwright's Chromium there.
    && if [ ! -x /opt/google/chrome/chrome ]; then playwright install --with-deps chromium; fi \
    && rm -rf /var/lib/apt/lists/*

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
    && useradd --uid 1000 --create-home --home-dir /home/flow --shell /usr/sbin/nologin flow \
    && mkdir -p /tmp/google-flow-mcp /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix \
    && chown -R 1000:1000 /tmp/google-flow-mcp /home/flow

# Obot runs MCP containers as UID 1000 with all capabilities dropped.
USER 1000
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fsS http://127.0.0.1:${PORT}/healthz || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["google-flow-mcp", "serve"]
