#!/usr/bin/env bash
# Regenerates the mock-mode HLS fixtures under mock/cdn (committed, ~310 KB). Needs ffmpeg with libx264 + drawtext.
# Plain HLS like the deployed channels: MPEG-TS segments, H.264, no EXT-X-MAP. But video only, where the channels mux
# AAC-LC into every segment: with muxed AAC (1024-sample frames, silent 48 kHz stereo) the SGAI break's buffered range
# had a 32-64 ms hole between its two ads, and hls.js 1.7.3 stalled at a 64 ms hole in 5 of 8 local Chromium runs
# (2026-10-17) after entering the break. Video-only clips played every break through. The e2e test only reaches the
# first ad, so it passes either way, but the mock break should play through. See README.md "Known player caveat".
# Layout mirrors the media CDN: assets/ (content + slate), ads/ (creatives, beacon pixel) and two channel playlists:
#   channel/ssai.m3u8  stitched ads behind EXT-X-DISCONTINUITY (what MediaTailor SSAI serves)
#   channel/sgai.m3u8  slate in the primary timeline + an HLS Interstitial DATERANGE with X-ASSET-LIST
#                      (what MediaTailor SGAI serves on a Channel Assembly origin, ad-insertion.md §3.3)
set -euo pipefail
cd "$(dirname "$0")/../mock/cdn"
rm -rf assets ads channel sgai
font=$(fc-match -f '%{file}' 'DejaVu Sans:bold')

clip() {  # clip <dir> <seconds> <background> <label>
  local dir=$1 secs=$2 bg=$3 label=$4
  mkdir -p "$dir"
  ffmpeg -loglevel error -y \
    -f lavfi -i "color=c=${bg}:s=320x180:r=30:d=${secs}" \
    -vf "drawtext=fontfile=${font}:text='${label}':fontcolor=white:fontsize=22:x=(w-tw)/2:y=62,drawtext=fontfile=${font}:text='%{pts\:hms}':fontcolor=white@0.8:fontsize=16:x=(w-tw)/2:y=102" \
    -c:v libx264 -profile:v main -preset veryslow -crf 34 -g 60 -keyint_min 60 -sc_threshold 0 -bf 0 -pix_fmt yuv420p \
    -f hls -hls_time 2 -hls_playlist_type vod -hls_segment_type mpegts \
    -hls_segment_filename "${dir}/%03d.ts" "${dir}/index.m3u8"
}

clip assets/program 28 '#0f1b2a' 'PROGRAM'
clip assets/slate 8 '#414d5c' 'AD BREAK SLATE'
clip ads/cr-a 4 '#ec7211' 'AD A'
clip ads/cr-b 4 '#0972d3' 'AD B'
printf 'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;' > ads/b.gif

segments() {  # segments <dir> <first> <last>
  for i in $(seq "$2" "$3"); do printf '#EXTINF:2.000,\n../%s/%03d.ts\n' "$1" "$i"; done
}
header() {
  printf '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-MEDIA-SEQUENCE:0\n'
}

mkdir -p channel sgai
{
  header
  printf '#EXT-X-PROGRAM-DATE-TIME:2026-10-16T12:00:00.000Z\n'
  segments assets/program 0 5
  for ad in cr-a cr-b; do
    printf '#EXT-X-DISCONTINUITY\n'
    segments "ads/$ad" 0 1
  done
  printf '#EXT-X-DISCONTINUITY\n'
  segments assets/program 6 13
  printf '#EXT-X-ENDLIST\n'
} > channel/ssai.m3u8

{
  header
  printf '#EXT-X-PROGRAM-DATE-TIME:2026-10-16T12:00:00.000Z\n'
  segments assets/program 0 5
  printf '#EXT-X-DISCONTINUITY\n'
  printf '#EXT-X-DATERANGE:ID="1792152012000-34-1",CLASS="com.apple.hls.interstitial",START-DATE="2026-10-16T12:00:12.000Z",DURATION=8.0,PLANNED-DURATION=8.0,X-ASSET-LIST="../sgai/assetlist.json",X-SNAP="OUT,IN"\n'
  printf '#EXT-X-PROGRAM-DATE-TIME:2026-10-16T12:00:12.000Z\n'
  segments assets/slate 0 3
  printf '#EXT-X-DISCONTINUITY\n#EXT-X-PROGRAM-DATE-TIME:2026-10-16T12:00:20.000Z\n'
  segments assets/program 6 13
  printf '#EXT-X-ENDLIST\n'
} > channel/sgai.m3u8

# Asset list with client-side TRACKING, the shape MediaTailor returns for aws.reportingMode=CLIENT (ad-insertion.md §3.3).
# Root-relative URIs: hls.js resolves asset URIs against the page, not the asset list (MediaTailor writes absolute ones).
tracking() {  # tracking <creative> <ad start s>
  local cr=$1 t=$2 events=""
  for pair in impression:0 start:0 firstQuartile:1 midpoint:2 thirdQuartile:3 complete:4; do
    events+="{\"eventType\":\"${pair%%:*}\",\"startTimeInSeconds\":$((t + ${pair##*:})),\"beaconUrls\":[\"/mock/cdn/ads/b.gif?evt=${pair%%:*}&cr=${cr}&cb=[CACHEBUSTING]\"]},"
  done
  printf '{"adId":"%s","creativeId":"%s","vastAdId":"ad-%s","durationInSeconds":4.0,"startTimeInSeconds":%s,"trackingEvents":[%s]}' \
    "$cr" "$cr" "$cr" "$t" "${events%,}"
}
cat > sgai/assetlist.json <<JSON
{"ASSETS":[{"URI":"/mock/cdn/ads/cr-a/index.m3u8","DURATION":4.0},{"URI":"/mock/cdn/ads/cr-b/index.m3u8","DURATION":4.0}],
 "TRACKING":{"avails":[{"availId":"1792152012000-34-1","ads":[$(tracking cr-a 12),$(tracking cr-b 16)]}]}}
JSON
rm assets/*/index.m3u8
du -sh .
