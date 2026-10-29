FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    USER=crypttube

# VAAPI drivers for GPU transcoding (AMD: mesa; Intel: iHD, which needs non-free).
# NVIDIA's libraries come from the host via the NVIDIA container toolkit.
# Retry downloads: a mirror hiccup during a deploy shouldn't fail the whole build.
RUN printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\n' > /etc/apt/apt.conf.d/80-retries \
    && sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
    && { grep -q non-free /etc/apt/sources.list.d/debian.sources || { echo 'could not enable non-free'; exit 1; }; } \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
        mesa-va-drivers intel-media-va-driver-non-free \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ARG INSTALL_DEV=0
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt \
    && if [ "$INSTALL_DEV" = "1" ]; then pip install -r requirements-dev.txt; fi

COPY . .

# Default non-root user; compose overrides the uid/gid with PUID/PGID. A new named
# volume mounted here copies this directory's permissions, so any PUID can write.
RUN mkdir -p /data/generated /uploads && chmod 777 /data/generated /uploads
USER 1000:1000

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
