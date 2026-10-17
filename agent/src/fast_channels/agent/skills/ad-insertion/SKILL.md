---
name: ad-insertion
description: MediaTailor server-side ad insertion (SSAI) for FAST channels - the playback configuration, preconditioned ads, ad decision URLs and the playback URL per output. Use when the operator asks how ads work, why an ad did not play, or which URL to give a player.
---
# Ad insertion

## SSAI only
- Each channel has one MediaTailor playback configuration in **STITCHED_ONLY** mode: server-side ad insertion.
  MediaTailor stitches the ads into each viewer's stream, which plays on every HLS player.
- There is no server-guided ad insertion (SGAI, HLS Interstitials) and there are no DASH URLs; do not offer them.
- `channels___get_channel` returns one playback URL per output through the primary CDN, and `cdn_playback` with
  the same URLs through every other configured CDN. Give operators those exact URLs; never build URLs yourself.

## Preconditioned ads
- Ads are encoded on the channel's own ladder ahead of time (house ads in the ladder kit), and MediaTailor
  plays them without transcoding, so every variant gets the full ad.
- A creative seen for the first time can be skipped once while MediaTailor prepares it; a missing first ad
  after provisioning is expected.
- An operator's own ad server must return ads encoded on the channel's ladder (the same variants), or use the
  external VAST mode, where MediaTailor transcodes the ads.

## Ad decision requests
- The ad server URL carries the channel, the MediaTailor session id, the SCTE event id, the content UPID and
  the break duration, so every ad request can be traced to a program and break.
- MediaTailor fires the ad beacons server-side as the player fetches the ad segments.

## Troubleshooting
- No ads at all: check the channel is running (`channels___get_channel`), the program has breaks
  (`schedule___get_schedule`), and `origins___list_alerts` for the channel.
- A player that asks for server-guided insertion gets HTTP 400; give it the playback URL from
  `channels___get_channel` as it is.
