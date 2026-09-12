#!/usr/bin/env bash
# Ten-second synthetic clips named as real titles. Plex matches on the filename,
# so its real TMDB/Plex-agent pipeline engages and we get genuine guids, posters
# and episode metadata on the reference server -- with no real media on disk.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)/media"

mk() { # mk <path> <label>
  local out="$1" label="$2"
  [ -f "$out" ] && return 0
  mkdir -p "$(dirname "$out")"
  ffmpeg -y -loglevel error \
    -f lavfi -i "testsrc2=size=1920x1080:rate=24:duration=10" \
    -f lavfi -i "sine=frequency=440:duration=10" \
    -vf "drawtext=text='${label}':fontcolor=white:fontsize=64:x=(w-text_w)/2:y=(h-text_h)/2" \
    -c:v libx264 -preset veryfast -profile:v high -level 4.0 -pix_fmt yuv420p \
    -c:a aac -b:a 128k -shortest "$out"
}

# Blender open movies -- all real TMDB entries
mk "$ROOT/movies/Sintel (2010)/Sintel (2010) - 1080p.mkv"                   "Sintel"
mk "$ROOT/movies/Big Buck Bunny (2008)/Big Buck Bunny (2008) - 1080p.mkv"   "Big Buck Bunny"
mk "$ROOT/movies/Tears of Steel (2012)/Tears of Steel (2012) - 1080p.mkv"   "Tears of Steel"
mk "$ROOT/movies/Elephants Dream (2006)/Elephants Dream (2006) - 1080p.mkv" "Elephants Dream"

# A show with two seasons, so season/episode hierarchy is exercised
for s in 1 2; do
  for e in 1 2 3; do
    mk "$ROOT/shows/Firefly (2002)/Season 0$s/Firefly (2002) - s0${s}e0${e} - 1080p.mkv" "Firefly S0${s}E0${e}"
  done
done
echo "media tree:"
find "$ROOT" -name '*.mkv' | sort | sed "s|$ROOT/||"
