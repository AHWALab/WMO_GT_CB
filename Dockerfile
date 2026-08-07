# ============================================================================
# TITO — Real-time Flash Flood Forecasting System
# Slim Docker image (no PyTorch/CUDA; ops = STREAM-Sat + StormLab + EF5)
# ============================================================================
# Build:
#   docker build -t tito:latest .
#
# Run (operational):
#   docker run --rm \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     -v $(pwd)/EF5_conf:/app/EF5_conf \
#     -v $(pwd)/outputs:/app/outputs \
#     tito:latest
# ============================================================================

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

# System deps — geospatial + GRIB only (no GUI / OpenCV / CUDA toolkit)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    ca-certificates \
    git \
    # Docker CLI only (spawn sibling EF5 containers via docker.sock)
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Miniconda
RUN wget -q https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh \
        -O /tmp/miniconda.sh \
    && bash /tmp/miniconda.sh -b -p /opt/conda \
    && rm /tmp/miniconda.sh \
    && /opt/conda/bin/conda init bash

ENV PATH=/opt/conda/bin:$PATH

COPY tito_env.yml /tmp/tito_env.yml
RUN conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main \
    && conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r \
    && conda env create -f /tmp/tito_env.yml \
    && conda clean -afy \
    && find /opt/conda -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true \
    && rm -rf /opt/conda/pkgs

SHELL ["/bin/bash", "-c"]

WORKDIR /app
COPY . /app/

RUN mkdir -p /app/EF5_conf/basic /app/EF5_conf/parameters /app/EF5_conf/pet \
             /app/EF5_conf/templates /app/EF5_conf/states \
             /app/EF5_conf/precip /app/EF5_conf/precipEF5 /app/EF5_conf/qpf_store \
             /app/outputs /app/outputs/logs \
             /app/tito_utils/qpf_utils/StormLab-GFS-realtime/output \
             /app/EF5_conf/precip/stormlab /app/EF5_conf/precip/stream_sat \
             /app/EF5_conf/states/stream_sat \
             /app/EF5_conf/states/scampr /app/EF5_conf/states/hsaf \
             /app/outputs/stream_sat /app/outputs/scampr \
             /app/outputs/stormlab

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["operational"]
