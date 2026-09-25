#!/bin/zsh
# fresh.sh <serial> <label> <name> <machine> <uri> <token> <version>
H=$(cd "$(dirname "$0")" && pwd)
C=${DROID_WORK:-$HOME/plexsim-android}
mkdir -p $C/N/cap $C/N/out $C/N/shots
S=$1; shift
E(){ adb -s $S "$@"; }
E shell am force-stop com.plexapp.android
$H/restart_mitm.sh "$@"
E shell pm clear com.plexapp.android >/dev/null
E shell pm grant com.plexapp.android android.permission.POST_NOTIFICATIONS
python3 $H/signin.py $S
sleep 8
python3 $H/ui.py $S shot $C/N/shots/$1-home.png
