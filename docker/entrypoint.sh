#!/bin/sh
# Flags alone start the server on /config/rex.db (seeded on the first start);
# a subcommand such as `catalog --seed ...` runs as given.
set -e
# rex.db holds each source's login and claim_state.json the server token: owner only.
umask 077
case "${1:-}" in
  "" | -*) exec /rex/rust/target/release/rex --catalog "${REX_CATALOG:-/config/rex.db}" "$@" ;;
  *) exec /rex/rust/target/release/rex "$@" ;;
esac
