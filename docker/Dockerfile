# The image packages a release's own Linux builds (docker.yml unpacks them into dist/<arch>/),
# so it carries exactly the binary the release tested and compiles nothing under emulation.
# bookworm's glibc 2.36 is newer than the 2.35 the binaries link against.
FROM debian:bookworm-slim

ARG TARGETARCH

# ca-certificates for TMDB, plex.tv and HTTPS sources; sqlite3 for `.backup` of rex.db.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates sqlite3 \
 && rm -rf /var/lib/apt/lists/*

# The release layout: rust/target/release/rex and data/ beside it, which rex finds from its
# working directory while the catalog, its state and claim_state.json live in /config.
COPY dist/${TARGETARCH}/ /rex/
COPY entrypoint.sh /rex/entrypoint.sh

WORKDIR /rex
VOLUME /config
EXPOSE 32400

# rex saves its state on SIGTERM itself, so it runs as PID 1 with no init in front.
ENTRYPOINT ["/rex/entrypoint.sh"]
