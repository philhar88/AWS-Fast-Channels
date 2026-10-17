# Migrating from v2

v3 is a rewrite, not an upgrade. It shares no CloudFormation resources with v2, so there is no in-place update: deploy
v3 next to v2, move your content and channels over, then delete the v2 stack. The v2 code stays available on the
`release/2.x` branch and the `v2.0.0` tag.

- [What changed](#what-changed)
- [v2 parameters and their v3 equivalents](#v2-parameters-and-their-v3-equivalents)
- [Migration steps](#migration-steps)
- [Removing the v2 SAM stack](#removing-the-v2-sam-stack)

## What changed

| Area | v2 (SAM) | v3 (CDK) |
|---|---|---|
| Infrastructure as code | One SAM template plus a nested MediaConvert application; `sam deploy --guided` | Python CDK app in a uv workspace; `make deploy`; stacks `FastChannels-{Data,Media,Operator}` ([architecture](architecture.md)) |
| Runtime | Python 3.12 Lambda functions with boto3, crhelper and xmltodict layers | Python 3.14 arm64 Lambda functions from one bundle, Powertools, Step Functions with JSONata |
| Packaging | MediaConvert outputs re-packaged by MediaPackage VOD (HLS, DASH and CMAF packaging configurations); Channel Assembly read MediaPackage with a Secrets Manager token | One plain HLS encode per asset (MediaConvert Apple HLS group, MPEG-TS segments, AVC with AAC muxed); Channel Assembly reads the media bucket directly with SigV4. No MediaPackage, Secrets Manager or KMS key |
| Ladder | Generated from `assets/presets.csv`; AVC and HEVC up to 2160p | A validated TOML ladder, `standard` (AVC with muxed AAC to 1080p, MediaConvert Basic tier); fingerprinted versions ([encoding](encoding.md)) |
| Ad breaks | `AdOffsets` S3 tag, ESAM for every offset | Cue points from a sidecar, the catalog or the same `AdOffsets` tag, snapped to the 6 s segment grid, no ESAM ([ADR-0008](adr/0008-ad-break-alignment.md)) |
| Ads | `AdServerUrl` parameter (a demo VAST by default), creatives transcoded by MediaTailor | Preconditioned house ads per ladder and a static VAST pod by default; `ads.mode: external_vast` for your own server ([ad insertion](ad-insertion.md)) |
| Ad insertion | SSAI | SSAI and SGAI (HLS Interstitials) from one `PLAYER_SELECT` playback configuration per channel |
| Channels | One sample channel; programs scheduled by hand in the MediaTailor console | IaC channels from `cdk.json`, agent-created channels, schedules planned and published by the agent's tools, autopilot, EPG feeds ([Channel Assembly](channel-assembly.md)) |
| Operation | AWS console | Operator console with the scheduling agent on Amazon Bedrock AgentCore, Cognito users and Cedar policies ([agent](agent.md)) |
| Delivery | CloudFront in front of MediaPackage | CloudFront with OAC to S3 and ordered MediaTailor behaviours; a CDN-neutral path contract ([delivery](delivery-and-multi-cdn.md)) |
| Notifications | An email per processed asset with playback URLs | Domain events on an EventBridge bus; SNS alerts for failures and alarms; playback URLs in stack outputs, the console and `channels___get_channel` |
| Observability | X-Ray tracing, Lambda logs | Every service logs with retention, one correlation id from upload to ad request, saved queries, `scripts/trace.py`, dashboard and alarms ([observability](observability.md)) |
| Teardown | Empty buckets, delete VOD sources and MediaPackage assets by hand, then `sam delete` | `make destroy`; a reaper removes runtime resources first |

## v2 parameters and their v3 equivalents

| v2 parameter | v3 |
|---|---|
| `EmailAddress` | `notifications.email` (alarms and failures; there is no per-asset email) |
| `S3BucketName` | Not needed: bucket names are generated; `prefix` names everything else |
| `AdServerUrl` | `ads: {"mode": "external_vast", "url": "…"}`, or keep the default preconditioned pod |
| `AdOffsetS3TagKeyName` | Fixed: the `AdOffsets` tag (any case, also `ad_offsets`); a sidecar's `cue_points_ms` takes precedence |
| `LogLevel` | Functions log at INFO in JSON |
| `Enable4KEncoding` | None: v3 ladders top out at 1080p ([ADR-0016](adr/0016-plain-hls-ts-avc-aac-basic-tier.md)) |
| `EnableHEVC` | None: v3 encodes AVC only ([ADR-0016](adr/0016-plain-hls-ts-avc-aac-basic-tier.md)) |

## Migration steps

1. **Deploy v3** in the same account and region (or another). The names do not collide with v2's. Both deployments
   count against the account's Channel Assembly quotas (100 channels, manifest requests).
2. **Re-encode the library.** v2 outputs cannot be reused: v3 schedules only assets whose renditions match a v3 ladder
   exactly, and v2 content was packaged by MediaPackage from a different ladder. Copy the mezzanines from v2's input
   bucket (`<S3BucketName>-input-…`) into v3's ingest bucket (output `IngestBucketName`); each copy starts an ingest.
   - Write a sidecar `<key>.json` first when you want catalog ids or titles (`{"content_id": "…", "metadata": {"title": "…"}}`);
     see [integrate-your-cms.md](integrate-your-cms.md#ingest-contract).
   - `AdOffsets` tags keep working when the copy preserves tags (`aws s3 cp` and `aws s3 sync` copy tags by default with
     AWS CLI v2's `--copy-props default`).
   - Budget the encode: about $0.0375 per content minute ($2.25 per hour) on `standard`. The pipeline runs 20
     MediaConvert jobs at a time and queues the rest.
   - Sources at 25 or 50 fps are refused by the 29.97 fps `standard` ladder; add a 50 Hz ladder first
     ([encoding.md](encoding.md#frame-rates-interlace-and-hdr)).
3. **Load the catalog** so the agent can pick titles: write your titles into the `ReferenceCatalog` table, or put
   your CMS behind the Gateway ([integrate-your-cms.md](integrate-your-cms.md)).
4. **Recreate channels.** Add them to `channels.iac` in `cdk.json` (names start with `<prefix>-`), give each a
   programming brief, deploy, and have the agent build their schedules. v2 programs are not migrated; Channel Assembly
   deletes aired programs anyway.
5. **Ads.** To keep your ad decision server, use `external_vast` (MediaTailor transcodes its creatives), or deliver
   creatives preconditioned on the channel's ladder ([ad-insertion.md](ad-insertion.md#your-own-ad-server-with-preconditioned-creatives)).
6. **Switch players** to the v3 playback URLs (`Channels` output of the Media stack, or the console). v3 URLs are on the
   v3 CloudFront domain, or on your `delivery.content_domain`.
7. **Delete v2** once nothing plays from it.

## Removing the v2 SAM stack

v2's stack cannot delete a source location that still has VOD sources, or a packaging group that still has assets, so
empty them first. The source location and the sample channel are named after the stack.

```bash
STACK=<your v2 stack name>
CHANNEL=$(aws mediatailor list-channels --query "Items[?starts_with(ChannelName, '$STACK')].ChannelName" --output text)
SOURCE=$(aws mediatailor list-source-locations \
  --query "Items[?starts_with(SourceLocationName, '$STACK')].SourceLocationName" --output text)

aws mediatailor stop-channel --channel-name "$CHANNEL"
for p in $(aws mediatailor get-channel-schedule --channel-name "$CHANNEL" --duration-minutes 20160 \
             --query "Items[?ProgramName].ProgramName" --output text); do
  aws mediatailor delete-program --channel-name "$CHANNEL" --program-name "$p"
done
for v in $(aws mediatailor list-vod-sources --source-location-name "$SOURCE" \
             --query "Items[].VodSourceName" --output text); do
  aws mediatailor delete-vod-source --source-location-name "$SOURCE" --vod-source-name "$v"; sleep 1
done
for a in $(aws mediapackage-vod list-assets --packaging-group-id "$STACK-PackagingGroup" \
             --query "Assets[].Id" --output text); do
  aws mediapackage-vod delete-asset --id "$a"
done
aws s3 rm "s3://<S3BucketName>-input-<suffix>" --recursive    # after the mezzanines are copied to v3
aws s3 rm "s3://<S3BucketName>-output-<suffix>" --recursive
sam delete --stack-name "$STACK"
```

The v2 stack's own cleanup handlers stop and delete the sample channel; the commands above make sure nothing blocks
them. Programs scheduled by hand on other channels that read the v2 source location must be deleted the same way.
