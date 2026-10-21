# The toolchain CI builds and tests rex with on the self-hosted dmm runner (ci.yml). dmm is a
# production host, so the compilers live in this image instead of on it. Ubuntu 22.04 keeps the
# Linux builds on glibc 2.35. macOS is cross-built with cargo-zigbuild: zig supplies the linker
# and the system library stubs, so no Apple SDK is needed.
FROM ubuntu:22.04
# Execute the recorded Plex Web gate unchanged, including its ES2020 syntax.
COPY --from=node:24-bookworm-slim /usr/local/bin/node /usr/local/bin/node
# The fonts are the transcoder suite's: with none, libass burns no subtitle into the picture.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      ca-certificates curl git python3 python3-pip file xz-utils zip unzip openssl \
      build-essential cmake pkg-config \
      gcc-aarch64-linux-gnu libc6-dev-arm64-cross gcc-mingw-w64-x86-64 \
      fontconfig fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
ENV RUSTUP_HOME=/opt/rustup PATH=/opt/cargo/bin:$PATH
RUN curl -sSf https://sh.rustup.rs | CARGO_HOME=/opt/cargo sh -s -- -y -q --profile minimal \
      --target aarch64-unknown-linux-gnu,x86_64-pc-windows-gnu,aarch64-apple-darwin,x86_64-apple-darwin \
    && chmod -R a+rX /opt/rustup /opt/cargo
# 0.19.8 with zig 0.13 failed rex's macOS link without a word and needed aws-lc's CPU named.
RUN pip3 install -q --no-cache-dir cargo-zigbuild==0.23.4 ziglang==0.16.0
ENV CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
    CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc \
    CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=x86_64-w64-mingw32-gcc \
    QEMU_LD_PREFIX=/usr/aarch64-linux-gnu
