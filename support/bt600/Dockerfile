# syntax=docker/dockerfile:1.7
# Debian 13 (trixie) arm64 image published 2026-08-03, pinned by platform manifest digest.
FROM --platform=linux/arm64 debian@sha256:c94f5ddd41327aa2d4a7cfba7889056c02936182fd76a513fec6160c97181fc0

ARG DEBIAN_FRONTEND=noninteractive

# snapshot.debian.org is immutable. Package indexes remain authenticated even though this build
# uses HTTP to bootstrap ca-certificates in the minimal base image.
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260803T000000Z trixie main' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20260803T000000Z trixie-security main' \
        > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
        bc \
        binutils=2.44-3 \
        ca-certificates \
        dpkg-dev \
        file \
        gcc-14=14.2.0-19 \
        kmod \
        libelf-dev \
        libssl-dev \
        make \
        patch \
        pahole \
        python3-minimal \
        xz-utils \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    SOURCE_DATE_EPOCH=1781013328

WORKDIR /workspace
ENTRYPOINT ["python3", "/workspace/support/bt600/build_package.py"]
