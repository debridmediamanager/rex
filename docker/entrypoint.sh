#!/bin/sh
# Flags alone start the server on /config/rex.db (seeded on the first start);
# a subcommand such as `catalog --seed ...` runs as given.
set -e
case "${1:-}" in
  "" | -*) exec /rex/rust/target/release/rex --catalog "${REX_CATALOG:-/config/rex.db}" "$@" ;;
  *) exec /rex/rust/target/release/rex "$@" ;;
esac
