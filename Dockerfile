# ============================================================================
# TITO — Real-time Flash Flood Forecasting System
# Docker image with full conda environment, Nowcast ML, and EF5 integration
# ============================================================================
# Build:
#   docker build -t tito:latest .
#
# Run (operational):
#   docker run --rm \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     -v $(pwd)/states:/app/states \
#     -v $(pwd)/outputs:/app/outputs \
#     -v $(pwd)/precip:/app/precip \
#     -v $(pwd)/qpf_store:/app/qpf_store \
#     tito:latest
#
# Run (hindcast):
#   docker run --rm \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     -v $(pwd)/states:/app/states \
#     -v $(pwd)/outputs:/app/outputs \
#     -v $(pwd)/precip:/app/precip \
#     -v $(pwd)/qpf_store:/app/qpf_store \
#     tito:latest hindcast "2025-11-16 00:00" "2025-11-17 20:00"
# ============================================================================

FROM ubuntu:22.04

# Prevent interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

# ── System dependencies ────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core tools
    wget \
    curl \
    git \
    ca-certificates \
    build-essential \
    # Needed by rasterio, GDAL, netCDF4
    libgdal-dev \
    libnetcdf-dev \
    libhdf5-dev \
    # Needed by cfgrib / eccodes
    libeccodes-dev \
    # Needed by psutil, OpenCV, etc.
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libfontconfig1 \
    # Docker CLI (so TITO can spawn EF5 Docker containers)
    docker.io \
    # Cleanup
    && rm -rf /var/lib/apt/lists/*

# ── Install Miniconda ──────────────────────────────────────────────────────
RUN wget -q https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh \
        -O /tmp/miniconda.sh \
    && bash /tmp/miniconda.sh -b -p /opt/conda \
    && rm /tmp/miniconda.sh \
    && /opt/conda/bin/conda init bash

ENV PATH=/opt/conda/bin:$PATH

# ── Create conda environment ───────────────────────────────────────────────
# Copy only the env file first (better layer caching)
COPY tito_env.yml /tmp/tito_env.yml
# Newer Miniconda requires accepting Anaconda ToS in non-interactive builds
RUN conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main \
    && conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r \
    && conda env create -f /tmp/tito_env.yml \
    && conda clean -afy

# Make conda activate available in non-interactive shells
SHELL ["/bin/bash", "-c"]

# ── Install Nowcast ML package ─────────────────────────────────────────────
COPY Nowcast/ /app/Nowcast/
RUN source /opt/conda/etc/profile.d/conda.sh \
    && conda activate tito_env2 \
    && cd /app/Nowcast/nowcasting \
    && pip install -e . --no-deps \
    && pip install requests

# ── Copy application code ──────────────────────────────────────────────────
WORKDIR /app
COPY . /app/

# Create directories that will be volume-mounted
RUN mkdir -p /app/states /app/outputs /app/precip /app/precipEF5 \
             /app/qpf_store /app/pet /app/basic /app/parameters \
             /app/templates /app/outputs/logs \
             /app/StormLab-GFS-realtime/output \
             /app/precip/stormlab /app/states/stream_sat \
             /app/states/scampr /app/states/hsaf \
             /app/outputs/stream_sat /app/outputs/scampr \
             /app/outputs/stormlab

# StormLab-GFS runs inside tito_env2 (same interpreter as TITO / STREAM-Sat).
# Code is under /app/StormLab-GFS-realtime; NC outputs bind-mounted at runtime.

# ── Entrypoint ─────────────────────────────────────────────────────────────
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["operational"]
