#!/usr/bin/env bash
# The lab's media, at real lengths, for plexscratch.
#
# plexlab's clips are ten seconds long. That is enough to identify titles, and too short
# for the playback surface: a real client's timeline beats carry its session header and a
# position that runs past a ten-second duration at once, so session-bound timelines,
# progress inside a session and a play with no duration cannot be measured on them.
#
# This writes media-long/ beside the script: every file of the lab's media tree, at the SAME
# relative path and in the same container, with the same stream layout (H.264 High@4.0
# yuv420p 1080p24 plus mono AAC) -- so the copied database's part paths still resolve --
# but 30 minutes long for a movie and 22 for an episode. One labelled minute is encoded
# per title at a low bitrate and looped by stream copy, which keeps the whole tree near
# 1 GB and the build to a couple of minutes. `./scratch.sh long` then points the copy at it.
set -euo pipefail
cd "$(dirname "$0")"

LAB=${PLEXLAB_DIR:-../plexlab}/media
OUT=$PWD/media-long
SEG=$PWD/media-long-seg
mkdir -p "$OUT" "$SEG"

cd "$LAB"
find . -type f \( -name '*.mkv' -o -name '*.mp4' \) | sort | while read -r f; do
  rel="${f#./}"; ext="${rel##*.}"
  case "$rel" in movies/*) total=1800 ;; *) total=1320 ;; esac
  label=$(basename "$rel" | sed 's/ - 1080p\..*//; s/[():]//g')
  seg="$SEG/$(echo "$rel" | tr '/ ' '__').$ext"
  dst="$OUT/$rel"
  [ -f "$dst" ] && { echo "exists $rel"; continue; }
  mkdir -p "$(dirname "$dst")"
  nice -n 19 ffmpeg -nostdin -y -loglevel error \
    -f lavfi -i "testsrc2=size=1920x1080:rate=24:duration=60" \
    -f lavfi -i "sine=frequency=440:duration=60" \
    -vf "drawtext=text='${label}':fontcolor=white:fontsize=64:x=(w-text_w)/2:y=(h-text_h)/2" \
    -c:v libx264 -preset veryfast -profile:v high -level 4.0 -pix_fmt yuv420p \
    -b:v 400k -maxrate 600k -bufsize 1200k -g 48 \
    -c:a aac -b:a 128k -shortest "$seg"
  nice -n 19 ffmpeg -nostdin -y -loglevel error -stream_loop $((total / 60 - 1)) -i "$seg" -c copy -map 0 "$dst"
  printf '%s  %s  %s\n' "$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$dst")" \
    "$(du -h "$dst" | cut -f1)" "$rel"
done
du -sh "$OUT"
