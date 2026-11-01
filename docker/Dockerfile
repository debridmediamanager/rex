# The image packages a release's own Linux builds (docker.yml unpacks them into dist/<arch>/),
# so it carries exactly the binary the release tested and compiles nothing under emulation.
# bookworm's glibc 2.36 is newer than the 2.35 the binaries link against.
FROM debian:bookworm-slim

ARG TARGETARCH
ARG JELLYFIN_FFMPEG=8.1.3-1

# ca-certificates for plex.tv and HTTPS sources; sqlite3 for `.backup` of rex.db.
# jellyfin-ffmpeg, the FFmpeg rex is measured with, where rex looks for it first
# (/usr/lib/jellyfin-ffmpeg), so conversion needs no setting; with /dev/dri passed in it uses
# the GPU through its own Intel driver. intel-opencl-icd (amd64; Intel's GPUs are x86) lets
# that GPU tonemap HDR too.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates sqlite3 curl \
 && curl -fsSL -o /tmp/jellyfin-ffmpeg.deb \
      "https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v${JELLYFIN_FFMPEG}/jellyfin-ffmpeg8_${JELLYFIN_FFMPEG}-bookworm_${TARGETARCH}.deb" \
 && apt-get install -y --no-install-recommends /tmp/jellyfin-ffmpeg.deb \
      $([ "$TARGETARCH" = amd64 ] && echo intel-opencl-icd) \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* /tmp/jellyfin-ffmpeg.deb

# The release layout: /rex/rex (its data/ compiled in), while the catalog, its
# state and claim_state.json live in /config.
COPY dist/${TARGETARCH}/ /rex/
COPY entrypoint.sh /rex/entrypoint.sh

WORKDIR /rex
VOLUME /config
EXPOSE 32400

# rex saves its state on SIGTERM itself, so it runs as PID 1 with no init in front.
ENTRYPOINT ["/rex/entrypoint.sh"]
