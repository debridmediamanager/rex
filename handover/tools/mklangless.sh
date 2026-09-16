#!/bin/bash
# Cycle 34: make a row whose audio tracks carry NO LANGUAGE, on plexscratch.
#
# zen 19813 is the only row in a real library with this shape -- a selected 2-channel mp3 and
# a 6-channel ac3, neither naming a language -- and it is read-only, so the decision rules it
# exposes could not be varied. This builds one on the writable copy instead. Run it ON KIDS.
#
#   ssh ben@kids 'bash -s' < mklangless.sh
#
# Why each flag:
#  * the video is COPIED from the lab's Sintel clip, so nothing about it is new to the scanner;
#  * an mp4 leaves an untagged track as `und`, which PMS reports as no language at all -- a
#    Matroska container writes `und` too but PMS reads it differently, so the container matters;
#  * BOTH tracks are marked `default`, as 19813's are;
#  * the 2-channel track is FIRST and the 6-channel one second, so "the selected track" and
#    "the widest track" are different streams -- the whole point of the row;
#  * `+faststart` sets `optimizedForStreaming=1`, which 19813 also carries. Without it the four
#    profiles whose mp4 ContainerProfile requires it refuse on that limitation before they ever
#    reach an audio check, and the cells that matter are never measured.
#
# Afterwards: `PUT /library/sections/{id}/refresh`, then `handover/tools/ddrive.py`. Remember
# the answer depends on per-user state the row does not show -- `PUT /library/parts/{id}?
# audioStreamID=<id>` sets a selection and `-1` clears it, and the 78 answers differ.
#
# To take it away again: delete the directory and refresh the section.
#
# Verified in cycle 34: re-running this over the row the cycle measured rebuilds it and all
# 78 profiles answer byte for byte what was recorded.
set -eu
SRC="/home/ben/plexscratch/media-long/movies/Sintel (2010)/Sintel (2010) - 1080p.mkv"
DIR="/home/ben/plexscratch/media-long/movies/Langless Probe (2019)"
OUT="$DIR/Langless Probe (2019).mp4"
mkdir -p "$DIR"
ffmpeg -y -i "$SRC" -f lavfi -i "anoisesrc=d=60:c=pink:r=44100" \
       -f lavfi -i "anoisesrc=d=60:c=pink:r=44100" \
       -map 0:v:0 -map 1:a:0 -map 2:a:0 \
       -c:v copy \
       -c:a:0 libmp3lame -b:a:0 128k -ac:a:0 2 \
       -c:a:1 ac3          -b:a:1 384k -ac:a:1 6 \
       -disposition:a:0 default -disposition:a:1 default \
       -movflags +faststart -t 60 \
       "$OUT"
