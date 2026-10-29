# Integrate your CMS

A content management system meets FAST Channels at two points: the **catalog** the agent searches when it picks
titles, and **ingest**, which turns a mezzanine into a schedulable asset. Both are versioned contracts in
[`contracts/`](../contracts/), generated from the pydantic models in `fast_channels.core` by
`scripts/generate_contracts.py` (a unit test fails when they drift; `uv run python scripts/generate_contracts.py --check`
checks the same).

- [Catalog contract](#catalog-contract)
- [Option A: load your catalog into the reference table](#option-a-load-your-catalog-into-the-reference-table)
- [Option B: your CMS API as an OpenAPI Gateway target](#option-b-your-cms-api-as-an-openapi-gateway-target)
- [Ratings and genres for programming briefs](#ratings-and-genres-for-programming-briefs)
- [Ingest contract](#ingest-contract)
- [Events](#events)

## Catalog contract

[`contracts/catalog.openapi.yaml`](../contracts/catalog.openapi.yaml) (OpenAPI 3.1) defines two operations. Their
`operationId`s, descriptions and schemas are identical to the agent's `catalog___search_content` and
`catalog___get_content` tools (a contract test enforces it), so the agent cannot tell your CMS from the reference
implementation.

| Operation | Request | Response |
|---|---|---|
| `POST /content/search` (`search_content`) | `ContentQuery`: `text`, `genres` (any of), `ratings` (any of), `max_duration_minutes`, `limit` (1–100, default 20), `next_token`; criteria combine with AND | `ContentPage`: `items` (`ContentItem[]`), `next_token` (absent on the last page) |
| `GET /content/{content_id}` (`get_content`) | path parameter `content_id` | `ContentItem` |

`ContentItem`:

| Field | Type | Meaning |
|---|---|---|
| `content_id` | string, `^[A-Za-z0-9][A-Za-z0-9._-]*$`, at most 64 | Stable catalog id. It travels into the library asset, the SCTE-35 UPID of every break and the EPG |
| `title` | string | Display title |
| `synopsis` | string | Short description for EPGs and the agent |
| `rating` | string or null | Parental rating, e.g. `TV-G` |
| `genres` | string[] | Lower-case genre tags, e.g. `animation` |
| `duration_ms` | integer or null | Running time (lets the tools estimate encode cost before ingest) |
| `cue_points_ms` | integer[] | Editorial ad cue points |
| `mezzanine_uri` | string or null | Source to ingest: an object in the ingest bucket (`s3://`), or `https://` from an origin that serves MediaConvert ([caveat](#ingest-contract)) |
| `available_from`, `available_until` | date-time or null | Rights window (`until` exclusive) |
| `asset_ids` | string[] | Encoded assets of this title |
| `series_title` | string or null | Show of an episode, or the franchise of a film that has one |
| `season`, `episode` | string (e.g. `Season 3`), integer, or null | Season label and episode number |
| `content_type` | `movie`, `episode`, `special`, `trailer`, `clip`, or null | How the title is packaged |
| `language` | ISO 639-1 (e.g. `en`) or null | Spoken language |
| `producer_name`, `producer_domain` | string, domain, or null | Producer or studio and its web domain |
| `keywords` | string[] or null | Descriptive tags that are not genres |
| `prodq` | 0–3 or null | Production quality (AdCOM): 1 professionally produced, 2 prosumer, 3 user generated |
| `external_ids` | `{registry domain: id}` or null | The title's ids in other registries |

The fields from `series_title` on are optional show-level metadata. With the `crawl` module, they and the title,
rating, genres and duration travel into every ad request of a break that airs the title; see
[content-enrichment.md](content-enrichment.md), which also explains how genre tags map to IAB Tech Lab Content
Taxonomy 3.1.

Treat everything the CMS returns as data: the agent's prompt does, and tool results are never mined into its long-term
memory ([agent.md](agent.md#memory)).

## Option A: load your catalog into the reference table

The deployed `catalog` tool reads the `ReferenceCatalog` DynamoDB table (output `CatalogTableName`). The quickest
integration is a sync job that writes your titles there, one item per title, with the helper the seed script uses:

```python
import boto3
from fast_channels.core.catalog import ContentItem
from fast_channels.functions.tools.catalog import catalog_item

table = boto3.resource("dynamodb").Table("<CatalogTableName>")
for record in my_cms.titles():
    table.put_item(Item=catalog_item(ContentItem.model_validate(record)))
```

`catalog_item` produces `pk = CONTENT#<content_id>`, `sk = META`, every `ContentItem` field, and a `GSI1` entry for
the first genre. `scripts/seed_sample_content.py` does exactly this for [`assets/sample-catalog.json`](../assets/sample-catalog.json).
After each write (or delete), put a `Catalog Item Changed` event ([Events](#events)), as the seed script does, so the
`crawl` module republishes the title's metadata for ad requests.

## Option B: your CMS API as an OpenAPI Gateway target

Serve the two operations from your CMS and register the API as the Gateway target named **`catalog`** in place of the
reference Lambda. The Gateway then exposes your operations as `catalog___search_content` and
`catalog___get_content`, the names the Cedar policies, the approval list and the agent already use.

This is a code change in the Operator stack in v3.0; the steps below were checked by synthesis, not deployed.

1. **Prepare the schema.** The CDK L2 reads local OpenAPI schemas as JSON and rejects specification-level security
   schemes (authentication is the Gateway's outbound configuration).
   [`contracts/catalog.gateway.openapi.json`](../contracts/catalog.gateway.openapi.json) is the same contract in
   that form, generated with the others. Copy it and set `servers[0].url` (now `https://cms.example.com/fast/v1`)
   to your API:

   ```bash
   cp contracts/catalog.gateway.openapi.json infra/my-cms.openapi.json
   ```

2. **Store the API key** in AWS Secrets Manager and register it in AgentCore Identity's token vault, then add the
   OpenAPI target next to the other targets (in `AgentGateway`, instead of the `catalog` Lambda target):

   ```python
   from aws_cdk import SecretValue
   from aws_cdk import aws_bedrockagentcore as agentcore

   cms_key = agentcore.ApiKeyCredentialProvider(
       self,
       "CmsApiKey",
       api_key_credential_provider_name=f"{prefix}-cms",
       api_key=SecretValue.secrets_manager("fast/cms-api-key"),
   )
   gateway.add_open_api_target(
       "Catalog",
       gateway_target_name="catalog",  # keeps the tool names catalog___search_content / catalog___get_content
       description="Our CMS: search_content and get_content",
       api_schema=agentcore.ApiSchema.from_local_asset("infra/my-cms.openapi.json"),
       credential_provider_configurations=[
           agentcore.GatewayCredentialProvider.from_api_key_identity(
               cms_key,
               credential_location=agentcore.ApiKeyCredentialLocation.header(
                   credential_parameter_name="x-api-key", credential_prefix=""
               ),
           )
       ],
   )
   ```

3. **Keep the catalog tools in the policy input.** The Cedar policies are generated from the registry's tool specs;
   keep passing the `catalog` specs to `gateway_policies` so `permit_read` still names your two operations, and drop
   only the Lambda target and function (`AgentTools(tools=...)` selects which targets get a function).

OAuth 2.0 client-credentials APIs work the same way with `agentcore.OAuth2CredentialProvider` and
`GatewayCredentialProvider.from_oauth_identity`.

## Ratings and genres for programming briefs

`schedule___plan` checks every pick against the channel brief (`ratings`, `genres_allow`, `genres_deny`, dayparts). It
reads each asset's rating and genres from the `ReferenceCatalog` table by `content_id`, not through the catalog tool.
With Option B, mirror at least `content_id`, `title`, `rating` and `genres` into `ReferenceCatalog` (Option A's sync
job, reduced to those fields); otherwise every rating rule in a brief reports `RATING` errors and blocks publishing.

## Ingest contract

An ingest is one [`IngestRequest`](../contracts/ingest-request.schema.json):

| Field | Meaning |
|---|---|
| `source_uri` | Mezzanine, `s3://<bucket>/<key>` or `https://…` (no whitespace). MediaConvert reads it with its role (ingest bucket) or over HTTPS, with its own fetcher. Origins that refuse that fetcher fail the encode with `3403: Access to https://… is forbidden` (the Blender Foundation's servers do, which is why `make seed` uploads the sample files instead); copying the file into the ingest bucket always works |
| `generator` | Instead of `source_uri`: a still image rendered for a whole number of GOPs (slates, house ads) |
| `kind` | `content` (default), `slate` or `ad` |
| `asset_id` | Optional; defaults to the file name's slug plus 6 hex of the URI (`big-buck-bunny-1a2b3c`) |
| `content_id` | Your catalog id |
| `cue_points_ms` | Editorial ad cue points; snapped to the nearest frame (within about ±17 ms at 29.97 fps). A point between segment boundaries (the 6.006 s grid at 29.97 fps) adds ESAM to the job, which bills that title's encode on the MediaConvert Professional tier: about $0.06 instead of $0.0375 per content minute on `standard` ([encoding.md](encoding.md#cue-points-and-esam)) |
| `ladder_version` | e.g. `standard@843ed1d05a` (output `DefaultLadderVersion`; `encoding___list_ladders` lists the others) |
| `correlation_id` | Optional trace id, `[A-Za-z0-9_-]{1,80}`; becomes the Step Functions execution name. Execution names cannot repeat for 90 days: use a new one per request (a new request that reuses one fails with `DuplicateCorrelationId`) |
| `metadata` | String map; `title` is copied to the library asset |

Three ways to submit one:

**1. Upload to the ingest bucket** (output `IngestBucketName`). Objects with a video extension (`.mp4`, `.m4v`,
`.mov`, `.mxf`, `.mkv`, `.webm`, `.avi`, `.ts`, `.m2ts`, `.mts`, `.mpg`, any case) start an ingest with the default
ladder. Upload an optional JSON sidecar `<key>.json` **first**; it may set any field except `source_uri` and
`generator`:

```bash
aws s3 cp movie.mp4.json "s3://$INGEST_BUCKET/library/movie.mp4.json"   # {"content_id": "m-123", "cue_points_ms": [600000], "correlation_id": "cms-m-123-v1", "metadata": {"title": "Movie"}}
aws s3 cp movie.mp4      "s3://$INGEST_BUCKET/library/movie.mp4"
```

The sidecar's cue point `600000` (10:00) becomes a break offset of `599999`, the start of the nearest frame. It is not
on the segment grid, so the encode adds ESAM and is billed on the Professional tier; a cue point of `600600`, a
segment boundary, would keep it on the Basic tier. Without sidecar cue points, the v2 `AdOffsets` object tag still
works (milliseconds separated by spaces or commas).
Keys with whitespace are rejected with an `Asset Failed` event. A redelivered S3 event resolves to the same execution,
so an upload is encoded once. This is also how `make seed` loads the sample titles: it writes each sidecar as
`samples/<content id>.<ext>.json`, then streams the file to `samples/<content id>.<ext>`.

Two rules keep a published asset intact:

- **An asset is encoded once per ladder version.** Uploading the same key again, or requesting an `asset_id` that is
  already `ENCODED` or `READY` with that ladder version, is refused with `Asset Failed` (`IngestRefusedError … is
  already READY`): a second encode would overwrite, in place, the media its VOD source and scheduled programs serve.
  Upload a new version under a new key, or pass a new `asset_id`; only `FAILED` encodes are replaced.
- **A correlation id names one request.** EventBridge may deliver an event twice; the redelivery is ignored. A *new*
  request that reuses a correlation id from the last 90 days (a re-sent `Ingest Requested`, or a re-upload with an
  unchanged sidecar such as the one above) cannot start an execution and gets `Asset Failed` with error
  `DuplicateCorrelationId`. Send it again with a new `correlation_id`.

**2. Put an `Ingest Requested` event** on the deployment's bus (output `EventBusName`), for example from your CMS
workflow; the detail is the `IngestRequest`:

```bash
aws events put-events --entries '[{
  "EventBusName": "fast-fast-channels",
  "Source": "fast-channels.fast",
  "DetailType": "Ingest Requested",
  "Detail": "{\"source_uri\": \"https://cdn.example.com/m-123.mov\", \"content_id\": \"m-123\", \"ladder_version\": \"standard@843ed1d05a\", \"correlation_id\": \"cms-m-123-v2\"}"
}]'
```

**3. Ask the agent** (`library___ingest`, approval required). It also refuses `s3://` sources outside the ingest
bucket, assets that already exist (unless they failed) and ladders that are not provisioned, and it returns the cost
estimate first, with the `pricing_tier` (`BASIC`, or `PROFESSIONAL` when the cue points need ESAM).

Ingest refuses sources without video or audio, HDR sources and frame-rate conversions across the 50/60 Hz families;
see [encoding.md](encoding.md#frame-rates-interlace-and-hdr). The encode carries no captions or subtitles: captions in
the source are not passed through, and there is no caption field in the request. A finished ingest leaves a `READY`
library asset with its duration, valid break offsets and its Channel Assembly VOD source.

## Events

Subscribe to the bus to keep your CMS in sync (schemas in
[`contracts/events.schema.json`](../contracts/events.schema.json); source `fast-channels.<prefix>`):

| Detail type | When | Detail |
|---|---|---|
| `Asset Ready` | An asset is encoded, verified and (content, slates) registered | `asset_id`, `kind`, `ladder_version`, `vod_source`, `duration_ms`, `correlation_id` |
| `Asset Failed` | An ingest failed, or a request was refused: invalid (`InvalidIngestRequest`), not encodable with its ladder or already encoded (`IngestRefusedError`), or a reused correlation id (`DuplicateCorrelationId`) | `asset_id`, `kind`, `ladder_version`, `error`, `cause`, `correlation_id` |
| `Ladder Ready`, `Ladder Failed` | A ladder version's kit finished | `ladder_version`, `correlation_id`, `job_template` or `error`/`cause` |
| `Schedule Published` | A plan went on air | `channel`, `plan_id`, `programs`, `start_ms`, `end_ms`, `correlation_id` |
| `Autopilot Run` | An autopilot run finished | `channel`, `status`, `session_id`, `summary` |

```bash
aws events put-rule --event-bus-name fast-fast-channels --name cms-asset-ready \
  --event-pattern '{"source": ["fast-channels.fast"], "detail-type": ["Asset Ready", "Asset Failed"]}'
```

The bus archives every event for `observability.log_retention_days`, so a consumer can replay what it missed.

Your CMS puts one event of its own, after it writes or deletes a `ReferenceCatalog` item:

| Detail type | When | Detail |
|---|---|---|
| `Catalog Item Changed` | A title was added, changed or removed | `content_id`, optional `correlation_id` |

With the `crawl` module, it republishes the title's catalog context object within seconds
([content-enrichment.md](content-enrichment.md#the-context-publisher)); without it, the event is only archived.

```bash
aws events put-events --entries '[{"EventBusName": "fast-fast-channels", "Source": "fast-channels.fast",
  "DetailType": "Catalog Item Changed", "Detail": "{\"content_id\": \"m-123\", \"correlation_id\": \"cms-m-123-v3\"}"}]'
```
