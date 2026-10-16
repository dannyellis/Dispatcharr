# Define base image build arguments (must be before any FROM)
ARG REPO_OWNER=dispatcharr
ARG REPO_NAME=dispatcharr
ARG BASE_TAG=base

# --- Build frontend ---

FROM node:24 AS frontend-builder

WORKDIR /app/frontend
COPY ./frontend /app/frontend
# remove any node_modules that may have been copied from the host (x86)
RUN rm -rf node_modules || true; \
    npm install --no-audit --progress=false;
RUN    npm run build && \
    rm -rf node_modules .cache

# --- Redeclare build arguments for the next stage ---
ARG REPO_OWNER
ARG REPO_NAME
ARG BASE_TAG

# --- Final image based on the dynamic base ---
FROM ghcr.io/${REPO_OWNER}/${REPO_NAME}:${BASE_TAG} AS final
ENV VIRTUAL_ENV=/dispatcharrpy
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
# Comskip links distro libav/libva; FFmpeg uses the separate /usr/local stack.
ENV COMSKIP_LIBVA_DRIVERS_PATH=/usr/lib/x86_64-linux-gnu/dri

# Keep QSV runtime requirements with the application: dev builds can use a
# published release base that predates this driver installation.
RUN if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
    apt-get update \
    && apt-get install --no-install-recommends -y intel-media-va-driver-non-free \
    && test -r "$COMSKIP_LIBVA_DRIVERS_PATH/iHD_drv_video.so" \
    && rm -rf /var/lib/apt/lists/*; \
    fi

# --help can exit 2. Check the advertised options without requiring a GPU.
RUN set -eu; \
    status=0; \
    comskip --help > /tmp/comskip-help.txt 2>&1 || status=$?; \
    test "$status" -eq 0 || test "$status" -eq 2; \
    grep -q -- '--qsv' /tmp/comskip-help.txt; \
    grep -q -- '--hwassist' /tmp/comskip-help.txt; \
    rm /tmp/comskip-help.txt

WORKDIR /app

# Copy application code
COPY . /app
# Copy nginx configuration
COPY ./docker/nginx.conf /etc/nginx/sites-enabled/default
# Fix line endings and make entrypoint scripts executable
RUN for f in /app/docker/entrypoint*.sh; do \
    if [ -f "$f" ]; then \
    sed -i 's/\r$//' "$f" && chmod +x "$f"; \
    fi; \
    done
# Clean out existing frontend folder
RUN rm -rf /app/frontend
# Copy built frontend assets
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

# Add timestamp argument
ARG TIMESTAMP

# Update version.py with build timestamp if provided
RUN if [ -n "$TIMESTAMP" ]; then \
    echo "Updating timestamp to ${TIMESTAMP} in version.py" && \
    sed -i "s|__timestamp__ = None.*|__timestamp__ = '${TIMESTAMP}'    # Set during CI/CD build process|" /app/version.py && \
    cat /app/version.py; \
    fi

ENTRYPOINT ["/app/docker/entrypoint.sh"]
