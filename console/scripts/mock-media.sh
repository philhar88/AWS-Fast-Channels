#!/usr/bin/env bash
# Regenerates the mock-mode HLS fixtures under mock/cdn (committed). Needs ffmpeg with libx264, aac and drawtext.
# Plain HLS like the deployed channels: MPEG-TS segments, H.264 with AAC-LC muxed into every segment (silent 48 kHz
# stereo), no EXT-X-MAP.
# Layout mirrors the media CDN: assets/ (content), ads/ (house ad creatives) and the channel's SSAI playlist:
#   channel/hls.m3u8  what MediaTailor SSAI serves for the `hls` output of a Channel Assembly channel: the break's
#                     EXT-X-DATERANGE (AdMarkupType DATERANGE, SCTE35-OUT splice_insert, channel-assembly.md) with
#                     the stitched ads behind EXT-X-DISCONTINUITY in its place (e2e.md §4.3)
set -euo pipefail
cd "$(dirname "$0")/../mock/cdn"
rm -rf assets ads channel
font=$(fc-match -f '%{file}' 'DejaVu Sans:bold')

clip() {  # clip <dir> <seconds> <background> <label>
  local dir=$1 secs=$2 bg=$3 label=$4
  mkdir -p "$dir"
  ffmpeg -loglevel error -y \
    -f lavfi -i "color=c=${bg}:s=320x180:r=30:d=${secs}" \
    -f lavfi -i "anullsrc=r=48000:cl=stereo" -t "$secs" \
    -vf "drawtext=fontfile=${font}:text='${label}':fontcolor=white:fontsize=22:x=(w-tw)/2:y=62,drawtext=fontfile=${font}:text='%{pts\:hms}':fontcolor=white@0.8:fontsize=16:x=(w-tw)/2:y=102" \
    -c:v libx264 -profile:v main -preset veryslow -crf 34 -g 60 -keyint_min 60 -sc_threshold 0 -bf 0 -pix_fmt yuv420p \
    -c:a aac -profile:a aac_low -b:a 32k \
    -f hls -hls_time 2 -hls_playlist_type vod -hls_segment_type mpegts \
    -hls_segment_filename "${dir}/%03d.ts" "${dir}/index.m3u8"
}

clip assets/program 28 '#0f1b2a' 'PROGRAM'
clip ads/cr-a 4 '#ec7211' 'AD A'
clip ads/cr-b 4 '#0972d3' 'AD B'

segments() {  # segments <dir> <first> <last>
  for i in $(seq "$2" "$3"); do printf '#EXTINF:2.000,\n../%s/%03d.ts\n' "$1" "$i"; done
}

# The 8 s break 12 s in. Channel Assembly writes the DATERANGE ID as <break start epoch ms>-101-<SpliceEventId hex>;
# SCTE35-OUT is a splice_insert (event 0x22, out of network, break_duration 8 s, auto return) with its CRC-32.
mkdir -p channel
{
  printf '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-MEDIA-SEQUENCE:0\n'
  printf '#EXT-X-PROGRAM-DATE-TIME:2026-10-16T12:00:00.000Z\n'
  segments assets/program 0 5
  printf '#EXT-X-DISCONTINUITY\n'
  printf '#EXT-X-DATERANGE:ID="1792152012000-101-22",START-DATE="2026-10-16T12:00:12.000Z",DURATION=8.0,PLANNED-DURATION=8.0,SCTE35-OUT=0xFC302500000000000000FFF01405000000227FEFFE00000000FE000AFC800001010100005134C71F\n'
  printf '#EXT-X-PROGRAM-DATE-TIME:2026-10-16T12:00:12.000Z\n'
  segments ads/cr-a 0 1
  printf '#EXT-X-DISCONTINUITY\n#EXT-X-PROGRAM-DATE-TIME:2026-10-16T12:00:16.000Z\n'
  segments ads/cr-b 0 1
  printf '#EXT-X-DISCONTINUITY\n#EXT-X-PROGRAM-DATE-TIME:2026-10-16T12:00:20.000Z\n'
  segments assets/program 6 13
  printf '#EXT-X-ENDLIST\n'
} > channel/hls.m3u8
rm assets/*/index.m3u8 ads/*/index.m3u8
du -sh .
