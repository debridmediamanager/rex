#!/bin/zsh
# usage: mitm.sh <label> <name> <machine> <uri> <token> <version>
H=$(cd "$(dirname "$0")" && pwd)
C=${DROID_WORK:-$HOME/plexsim-android}
mkdir -p $C/N/cap $C/N/out $C/N/shots
set -a; . ${DROID_TOKENS:-$C/tokens.env}; set +a
exec ${MITMDUMP:-/opt/homebrew/bin/mitmdump} -q --listen-port 8899 --set block_global=false --set connection_strategy=lazy --set http2=false \
  -s $H/../scripts/tap.py -s $H/droid.py \
  --set taplog=$C/N/cap/$1.jsonl --set tname="$2" --set tmachine="$3" --set turi="$4" \
  --set ttoken="$5" --set tversion="$6" --set acct="$ACCOUNT_TOKEN" --set resfile=$C/resources.json --set rewrite="$(cat $C/N/rewrite.txt 2>/dev/null)"
