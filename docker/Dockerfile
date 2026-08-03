################################################################################
# EF5: Ensemble Framework for Flash Flood Forecasting — Docker image
# Standalone image for the EF5_GuatemalaTraining workspace.
#
# This is the same Alpine build used by TITO (TITO_GuatemalaTraining/EF5/docker
# Dockerfile). It compiles EF5 from the AHWALab fork on GitHub and produces the
# `ef5-container:latest` image. The container does NOT contain model data — all
# inputs/outputs are bind-mounted at runtime (see run_ef5.sh / README.md).
#
# Build (needs internet to clone EF5):
#   ./docker/build_ef5.sh --rebuild
#
# Reuse without compiling: the prebuilt archive docker/ef5-container.tar can be
# loaded with `./docker/build_ef5.sh --load` (no internet needed).
################################################################################

ARG DOCKER_BASE_OS="alpine:3.17"

FROM ${DOCKER_BASE_OS} AS ef5-image
LABEL nssl.wrdd.image.authors="jorge.duarte@noaa.gov"

# Bind-mount volumes (runtime): /data (inputs), /output (results), /conf (control)
VOLUME [ "/data" ]
VOLUME [ "/output" ]
VOLUME [ "/conf" ]

# Update the package index
RUN apk update

# Install build tools and EF5 dependencies
RUN apk add --no-cache git automake autoconf build-base libgeotiff-dev

# Clone EF5 from GitHub onto the ef5 folder in the image root
# Using the AHWALab fork (master branch) with latest features and fixes
WORKDIR /
RUN git clone https://github.com/AHWALab/EF5.git ./ef5

# Set the ef5 folder as the working directory
WORKDIR /ef5

# Compile and install EF5 with OpenMP support and optimizations
RUN autoreconf --force --install
RUN ./configure
RUN make -j"$(nproc)" CXXFLAGS="-O3 -fopenmp"

# Run EF5 with the control file mounted from ./conf
CMD ["/ef5/bin/ef5", "/conf/control.txt"]
