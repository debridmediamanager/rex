#!/bin/zsh
# emu.sh <avd> <port>  -- boot an AVD through the c70 proxy, detached
H=$(cd "$(dirname "$0")" && pwd)
C=${DROID_WORK:-$HOME/plexsim-android}
mkdir -p $C/N/cap $C/N/out $C/N/shots
export ANDROID_SDK_ROOT=/opt/homebrew/share/android-commandlinetools ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
python3 -c "
import subprocess,sys
log=open('$C/N/emu_$1.log','ab')
subprocess.Popen(['$ANDROID_SDK_ROOT/emulator/emulator','-avd','$1','-writable-system','-no-window','-no-audio','-no-boot-anim','-no-snapshot','-http-proxy','http://127.0.0.1:${3:-8899}','-port','$2'],stdout=log,stderr=log,start_new_session=True)"
for i in $(seq 1 60); do s=$(adb -s emulator-$2 shell getprop sys.boot_completed 2>/dev/null | tr -d '\r'); [ "$s" = 1 ] && break; sleep 4; done
adb -s emulator-$2 root >/dev/null; sleep 2
for k in window_animation_scale transition_animation_scale animator_duration_scale; do adb -s emulator-$2 shell settings put global $k 0; done
echo boot=$s
