FROM python:3.11

WORKDIR /app

# Install Node.js for DOCX conversion (reuses pytho-vite's agentService.js)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Create the non-root user EARLY so subsequent COPYs can `--chown` to it
# inline. The previous layout deferred a `chown -R appuser:appuser /app` to
# the final layer, which captured every file in /app (including the
# 1.32 GB Chromium tree under /app/ms-playwright) as a "modified" copy and
# duplicated ~1 GB into the image. CIS 4.1 (run as non-root) is still
# satisfied — we just chown at copy time instead of after the fact.
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

COPY --chown=appuser:appuser ./app/requirements.txt .

RUN pip install --upgrade pip && \
    pip3 install --no-cache-dir --upgrade -r requirements.txt

# Install Chromium + system libs for the in-process headless map renderer
# (utils/render/headless.py). PLAYWRIGHT_BROWSERS_PATH lands the browser
# under /app so it stays readable to the non-root appuser after the
# read-only rootfs takes effect at runtime. Files land root-owned at mode
# 755/644 — appuser can read+execute via the "other" bit, no chown needed.
ENV PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright
RUN playwright install --with-deps chromium

# Xvfb so headed WebKit can attach to a virtual display. xauth +
# libegl1 + libgles2 give us the dispatch libs the NVIDIA EGL
# implementation needs at runtime (libEGL_nvidia.so.0 is mounted in by
# nvidia-container-toolkit). This is small (~10 MB) — the heavy X server
# stack belongs in a dedicated render-agent service, not here.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        xvfb xauth \
        libegl1 libgles2 libglvnd0 libglx0 libgbm1 \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright WebKit alongside Chromium. WebKit on Linux uses
# WebKitGTK with libEGL/libGL directly (no ANGLE wrapper, no GLX-via-X
# requirement), making it the engine of last resort for headless WebGL
# on GB10 / NVIDIA open kernel module where Chromium's only allowed
# implementation (gl=egl-angle) can't reach the GPU without DRI3.
RUN playwright install --with-deps webkit

# nvidia-container-toolkit mounts libEGL_nvidia.so.0 into the container
# but does NOT install the libglvnd ICD config that tells libEGL which
# vendor implementation to dispatch to. Without this JSON, libglvnd
# returns "no EGL vendor" and Chromium reports VENDOR=0xffff + falls
# back to SwiftShader. Documented at NVIDIA/libglvnd issue #187 and
# NVIDIA/nvidia-docker issue #1520.
RUN mkdir -p /etc/glvnd/egl_vendor.d && \
    printf '%s\n' \
        '{' \
        '    "file_format_version" : "1.0.0",' \
        '    "ICD" : {' \
        '        "library_path" : "libEGL_nvidia.so.0"' \
        '    }' \
        '}' \
        > /etc/glvnd/egl_vendor.d/10_nvidia.json

# Tiktoken encoding cache (cl100k_base + o200k_base + p50k_base +
# r50k_base + gpt2 encoder/vocab) baked into the image so docker-api
# starts offline. Without this, tiktoken.get_encoding("cl100k_base") at
# module import time tries to fetch from
# openaipublic.blob.core.windows.net and crashes the container with
# "Temporary failure in name resolution". Cache filenames are sha1(URL)
# per tiktoken/load.py. Burned IBL deploy 2026-06-03.
COPY ./app/tiktoken-cache/ /opt/tiktoken-cache/
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache

# Force huggingface_hub + transformers + datasets into pure-cache mode.
# Without these, library code (tokenizer auto-resolve, image processor
# config, etc.) phones home even when the model is already on disk and
# crashes with the same DNS-resolution error as above. Set at image
# layer so EVERY pytho-app code path is offline-clean by default.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1
ENV HF_DATASETS_OFFLINE=1

# Container entrypoint starts Xvfb on :99 and execs the CMD. See
# entrypoint.sh for the full rationale.
COPY ./app/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

COPY --chown=appuser:appuser ./app/app .

# Install JS dependencies for DOCX conversion. node_modules lands
# root-owned but world-readable (default npm mode), which is fine — appuser
# only needs to require() the files at runtime, not write them.
COPY --chown=appuser:appuser ./app/app/docx-converter/package.json ./docx-converter/
RUN cd docx-converter && npm install --production

# Run as non-root user (CIS 4.1)
ENV HF_HOME=/app/models
ENV HF_HUB_DISABLE_PERMISSIONS_CHECK=1

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/')"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0"]
