#!/bin/zsh
# usage: restart_mitm.sh <label> <name> <machine> <uri> <token> <version>
H=$(cd "$(dirname "$0")" && pwd)
C=${DROID_WORK:-$HOME/plexsim-android}
mkdir -p $C/N/cap $C/N/out $C/N/shots
launchctl remove com.plexsim.c70mitm 2>/dev/null
pkill -f 'mitmdump -q --listen-port 8899'; sleep 1
launchctl submit -l com.plexsim.c70mitm -o $C/N/mitm.log -e $C/N/mitm.log -- /bin/zsh $H/mitm.sh "$@"
for i in 1 2 3 4 5 6 7 8 9 10; do lsof -iTCP:8899 -sTCP:LISTEN >/dev/null 2>&1 && break; sleep 1; done
