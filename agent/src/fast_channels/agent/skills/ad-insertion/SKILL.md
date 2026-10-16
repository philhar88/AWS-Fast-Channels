---
name: ad-insertion
description: MediaTailor ad insertion for FAST channels - SSAI versus SGAI (HLS Interstitials), preconditioned ads, ad decision URLs and the playback URLs per output. Use when the operator asks how ads work, why an ad did not play, or which URL to give a player.
---
# Ad insertion

## SSAI and SGAI from one configuration
- Each channel has one MediaTailor playback configuration in **PLAYER_SELECT** mode, so a single channel
  serves both:
  - **SSAI** (server-side, stitched): ads are spliced into the stream; works on every player, HLS and DASH.
  - **SGAI** (server-guided, HLS Interstitials): the playlist carries `EXT-X-DATERANGE` interstitials and the
    player fetches the ads itself. Same URL plus `?aws.insertionMode=GUIDED`. **HLS only**; DASH stays SSAI.
- `channels___get_channel` returns the playback URLs per output (`ssai`, `sgai`, `dash`) through the primary
  CDN, and `cdn_playback` with the same URLs through every other configured CDN. Give operators those exact
  URLs; never build URLs yourself.

## Preconditioned ads
- Ads are encoded on the channel's own ladder ahead of time (house ads in the ladder kit), and MediaTailor
  plays them without transcoding. This keeps ads full length in every AVC, HEVC and AV1 variant.
- A creative seen for the first time can be skipped once while MediaTailor prepares it; a missing first ad
  after provisioning is expected.
- External ad servers must return ads already conditioned to the ladder; mixed-codec outputs are not
  supported with external VAST.

## Ad decision requests
- The ad server URL carries the channel, the MediaTailor session id, the SCTE event id, the content UPID and
  the break duration, so every ad request can be traced to a program and break.

## Troubleshooting
- No ads at all: check the channel is running (`channels___get_channel`), the program has breaks
  (`schedule___get_schedule`), and `origins___list_alerts` for the channel.
- Ads in SSAI but not SGAI: the player must support HLS Interstitials (hls.js 1.6+, Shaka, Apple 16.4+).
