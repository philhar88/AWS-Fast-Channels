# 0031 — Show-level content metadata in ad requests, looked up by the break's UPID

**Status:** Proposed (the `crawl` module; accepted with the module's last change)

## Context
Buyers of connected-TV supply value programme-level signals: title, series, season, episode, genre, content category,
rating, length, language, network and channel. Sellers' specifications commonly list them as recommended for CTV and
warn that missing values reduce bid eligibility, and FAST supply often sends none. Our catalog already holds most of
them, and every ad break already names its title: `schedule___plan` writes the content id into the break's SCTE-35
MPU UPID (`FST3` + content id), and MediaTailor exposes it, with `FST3` stripped, as `scte.segmentation_upid` in the
ad decision server request.

Measurements made in an AWS account in October 2026 set the rules for using that id at break time:

- **Function cache.** A PRE_ADS `HTTP_REQUEST` with `Cache` (300 s, a custom key) served cold herds of 100 and 300
  sessions with exactly 4 upstream calls per key, whatever the arrival pattern or origin latency, and warm breaks with
  none. Concurrent misses are coalesced inside each of about 4 independent cache partitions per playback configuration,
  and sessions are not pinned to a partition. A per-viewer key would therefore hit about one time in four.
- **Budgets.** `LiveMaximumAdsPersonalizationTimeMilliseconds` is a wall-clock deadline that includes PRE_ADS time:
  the ADS call is cut, never the hook, and a hook that outlasts the deadline suppresses the ADS request without an
  error. A warm cache hit costs 8–10 ms of function time.
- **Output limits.** The documented 1,000-character limits on Function output values and expressions are not enforced
  by the service (2,000-character values were delivered).
- **Request templates.** MediaTailor inserts values into a POST body template without JSON escaping. A `"` or `\`
  in a value, or an unresolved unquoted number, makes the body invalid, and MediaTailor then skips the request
  silently.
- **Demand path.** Prebid Server Java 3.43.0 drops the OpenRTB 2.6 content fields `genres`, `gtax`, `realtime`,
  `firstbroadcast` and `data[].cids` at parse time without a warning; values under `content.ext` pass. 4.4.0 models all
  five.

## Decision
- **Catalog.** `ContentItem` gains optional show-level fields (`series_title`, `season`, `episode`, `content_type`,
  `language`, `producer_name`, `producer_domain`, `keywords`, `prodq`, `external_ids`). Existing catalogs stay valid
  and unchanged.
- **Taxonomy.** Genres and categories use IAB Tech Lab Content Taxonomy 3.1 ids, reached from catalog tags through IAB
  Tech Lab's CTV Genre Mapping (`core/crawl/taxonomy/genre-map.toml`; the id list includes the 3.x descriptive vectors,
  since the mapping uses one). Loading the map rejects unknown ids. A title gets at most three genres, most apt first;
  `cat` equals `genres`, `gtax` and `cattax` are always 9 explicitly, and tags with no CTV genre go to `kwarray`.
- **Catalog context object.** One JSON object per title, `catalog/ctx/v1/<content_id>.json` in a catalog-context
  bucket that the Media stack creates with the module, never in the media or the ingest bucket. CloudFront serves it
  on a `/catalog/ctx/*` route with `Cache-Control: max-age=300`. The bucket's policy lets that distribution list
  it, so a missing object is a 404, which the Functions cache stores (a 403 it does not, and CloudFront runs no
  viewer-response function on an origin 4xx); the route forwards no query string and only paths under
  `/catalog/ctx/`, so it can never return a listing. Its `v1`
  schema (`contracts/catalog-context.schema.json`) never changes: a new schema is `v2` under a new prefix with a new
  cache key, because cached Function responses cannot be invalidated. The builder enforces the limits itself: free
  text 200 characters, every value a Function may output (`hdr`, `title_q`, `series_q`) 1,000 characters, the object
  20,000.
- **Lookup.** On the monetized playback configurations, PRE_ADS maps a sequential executor (400 ms) whose one step is
  an `HTTP_REQUEST` GET of the context object (250 ms), cached 300 s under `meta|v1|<UPID>` and skipped when the break
  has no UPID. Its URL, headers and cache key hold only the UPID. It writes one output, the `x-fast-ctx` header from
  `hdr`, and nothing into `player_params`. `modules.crawl.pre_ads: false` turns the lookup off without losing the
  OpenRTB fields.
- **OpenRTB.** The Pod Decision Service fills `app.content` from the same objects through its policy patch: the 2.6
  fields in `content.ext` on Prebid Server 3.43.0 and natively from 4.4.0. Pseudonymous sessions get genres and
  categories but never the title. Neither catalog nor player values enter the POST body template.
- **Measurement.** A `crawl-v1` holdout (`modules.crawl.holdout_percent`, 20 by default) assigns each session-break to
  an arm by `SHA-256("crawl-v1" ‖ sid ‖ "." ‖ evid) mod 100`. The arm is in the decision record and our tracker, and
  an Athena view reports coverage, fill and price by arm.
- **Failure.** Enrichment fails open: an unknown id (a cached 404), an origin error or timeout, a missing UPID or an
  exhausted budget sends the request unenriched.

## Consequences
- The catalog origin sees about 4 requests per title per playback configuration in each 300 s window in which the
  title airs, not one per viewer. Metadata can be up to 300 s plus the CDN TTL stale.
- PRE_ADS time comes out of the ad request's budget, so the release validators require
  `LiveMaximumAdsPersonalizationTimeMilliseconds` ≥ the executor timeout + `AdsRequestTimeoutMilliseconds`.
- The lookup and the objects carry no viewer data. The bid request still pairs what is watched with the device
  signals of the demand path, which is why pseudonymous sessions send no title; real demand needs a legal review first.
- A taxonomy update is a data change: regenerate `ct31-ids.txt` as `ATTRIBUTION.md` describes and review the map.
- Ad servers that take metadata as URL parameters are a later recipe variant that writes `player_params.content_*`
  (the key names of the public MediaTailor Functions sample recipes' content outputs) from the precomputed `*_q`
  forms.
- See [content-enrichment.md](../content-enrichment.md).
