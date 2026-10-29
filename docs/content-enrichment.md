# Content enrichment: show-level metadata in ad requests

Every ad break names the title on air: Channel Assembly writes the title's content id into the break's SCTE-35
segmentation UPID (type `0x0C` MPU, format identifier `FST3`), and MediaTailor exposes it as
`scte.segmentation_upid` ([ad-insertion.md](ad-insertion.md#the-ad-decision-server-url)). The `crawl` module
(`modules.crawl`, [ADR 0031](adr/0031-show-level-content-metadata.md)) turns that id into the show-level metadata
buyers read: title, series, season, episode, genre and category in IAB Tech Lab Content Taxonomy 3.1, rating, length,
language and producer, from your own catalog. It uses no viewer data, adds a bounded latency and fails open: a break
whose metadata cannot be read is requested without it.

- [How it works](#how-it-works)
- [The context publisher](#the-context-publisher)
- [Serving and misses](#serving-and-misses)
- [Catalog fields](#catalog-fields)
- [The catalog context object](#the-catalog-context-object)
- [Genres and categories](#genres-and-categories)
- [The PRE_ADS lookup](#the-pre_ads-lookup)
- [Limits](#limits)
- [Privacy](#privacy)

## How it works

1. The catalog holds the metadata ([Catalog fields](#catalog-fields)); every field it adds is optional.
2. A context publisher writes one **catalog context object** per title to the catalog-context bucket at
   `catalog/ctx/v1/<content_id>.json`, served through the CDN with `Cache-Control: max-age=300`
   ([Serving and misses](#serving-and-misses)).
3. On each ad break, a cached PRE_ADS `HTTP_REQUEST` Function reads the object by the break's UPID and passes its
   compact form (`hdr`) to the ad decision server as a header. MediaTailor caches the response for 300 s per content id
   and playback configuration, so the origin sees about 4 requests per title per playback configuration in each
   300 s window, whatever the audience.
4. The Pod Decision Service reads the same objects and fills OpenRTB 2.6 `app.content` in each bid request.

The catalog fields, the context object (`fast_channels.core.crawl.catalog_context`), the genre map, the
[publisher](#the-context-publisher) and the [PRE_ADS lookup](#the-pre_ads-lookup) are in place; the OpenRTB mapping
is added with the rest of the module.

## The context publisher

With `modules.crawl.enabled`, the stack `FastChannels-<prefix>-Crawl` deploys a Lambda function that keeps the objects
in step with the reference catalog (`ReferenceCatalog`, [integrate-your-cms.md](integrate-your-cms.md#option-a-load-your-catalog-into-the-reference-table)).
An object exists while its title is in the catalog: it is written when the title appears or changes and deleted
when the title is removed, so a lookup of a removed title misses. Three triggers run it:

| Trigger | Titles |
|---|---|
| `Schedule Published` (the schedule tools publish a plan) | Every title of the plan's programs |
| `Catalog Item Changed` (your CMS sync job, or `make seed`) | The one title named in the event |
| Daily sweep (EventBridge Scheduler, `rate(1 day)`) | Every catalog title; objects of titles that left the catalog are deleted |

- After writing or deleting a `ReferenceCatalog` item, put a `Catalog Item Changed` event on the deployment's bus
  ([Events](integrate-your-cms.md#events)). The object follows within seconds. `make seed` announces the sample
  titles this way.
- An object is rewritten only when its content changes: the function compares a digest it keeps in the object's
  metadata, so `updated` is when the metadata last changed and an unchanged catalog costs one `HEAD` per title.
- Scheduler runs the first sweep when the schedule is created: on `fastm` it ran within seconds of the stack's
  creation, which published the catalog that was already loaded. To publish the whole catalog at any other time,
  invoke the sweep (the stack's `CatalogContextPublisherName` output):

  ```bash
  aws lambda invoke --function-name <CatalogContextPublisherName> --payload '{"sweep": true}' \
    --cli-binary-format raw-in-base64-out /dev/stdout
  ```

- The CDN serves the objects on its `/catalog/ctx/*` route (`CatalogContextBaseUrl` output:
  `<CdnBaseUrl>/catalog/ctx/v1/`) and keeps them for their `max-age` of 300 s, so a change reaches the lookups within
  about 5 minutes.
- `modules.crawl.publisher_domain` names your domain as the registry of your content ids (the first `cids` entry).
  Without it the objects carry the content id in `id` only.
- A catalog item that breaks the contract is logged as an error and counted in the `ContextsInvalid` metric
  (namespace `FastChannels`), and its object is left as it was. A trigger that still fails after its retries goes to
  a dead-letter queue, whose alarm `<prefix>-crawl-publisher-dead-letters` notifies the alert topic.
- The function can read the catalog and media library tables, and read, write, delete and list objects under
  `catalog/ctx/v1/` in the catalog-context bucket (`CatalogContextBucketName` output). It has no access to the media
  or the ingest bucket.
- One run of the sweep handles roughly as many titles as fit in its 5-minute timeout at one `HEAD` per title. A
  catalog of tens of thousands of titles needs a sweep that is split into pages.

## Serving and misses

With the module on, the Media stack adds a bucket of its own for the context objects, and a CloudFront route,
`/catalog/ctx/*`, ahead of the default route. The media bucket, its policy and every other route stay as they are.

A lookup of a title that is not in the catalog must come back as **404**: MediaTailor's Function cache stores a 404
(the miss is then served from the cache for 300 s, like a hit), but not a 403. S3 answers 404 for a missing key only
to a requester that may list the bucket, so:

- The bucket's policy lets this distribution, and only it (`AWS:SourceArn`), read `catalog/ctx/*` and list the
  bucket. The bucket is otherwise private: Block Public Access, SSE-S3, bucket owner enforced, TLS 1.2 or later only.
- The route forwards only what its cache key holds (`Managed-CachingOptimizedForUncompressedObjects`: no query string,
  header or cookie), has no origin request policy, accepts GET and HEAD only, and its origin adds no path. Every
  request S3 sees is therefore a `GetObject` (or `HeadObject`) of a key under `catalog/ctx/`: never the bucket root,
  never a listing, whatever the viewer appends.
- A viewer-response CloudFront Function cannot turn the media bucket's 403 into a 404: CloudFront does not run
  viewer-response functions when the origin answers 400 or more.

Other CDNs chained to CloudFront serve the route as the `PathContract` output describes it (origin `catalog`).

## Catalog fields

`ContentItem` ([integrate-your-cms.md](integrate-your-cms.md#catalog-contract)) has these optional fields for
enrichment, next to `title`, `rating`, `genres` and `duration_ms`:

| Field | Type | Context object | OpenRTB 2.6 `content` |
|---|---|---|---|
| `series_title` | string | `series` | `series`: the show of an episode, or the franchise of a film that has one |
| `season` | string, e.g. `Season 3` | `season` | `season` |
| `episode` | integer | `episode` | `episode` |
| `content_type` | `movie`, `episode`, `special`, `trailer`, `clip` | `type` | (packaging) |
| `language` | ISO 639-1, e.g. `en` | `language` | `language` |
| `producer_name`, `producer_domain` | string, domain | `producer` | `producer.name`, `producer.domain` |
| `keywords` | string[] | `kwarray` | `kwarray` |
| `prodq` | 0–3 (AdCOM: 1 professionally produced, 2 prosumer, 3 user generated) | `prodq` | `prodq` |
| `external_ids` | `{registry domain: id}` | `cids` | `data[].cids` |

The existing fields map too: `content_id` → `id`, `title` → `title`, `rating` → `contentrating`, `duration_ms` →
`len` (seconds, rounded), and `genres` → `genre`, `genres` and `cat` through the [genre map](#genres-and-categories).

## The catalog context object

The context object of a title in the sample catalog (the publisher's domain is a deployment setting):

```json
{"v": 1, "id": "tears-of-steel", "title": "Tears of Steel", "series": "", "season": "", "episode": null,
 "type": "movie", "genre": "Science Fiction", "genres": ["652", "325"], "gtax": 9, "cat": ["652", "325"], "cattax": 9,
 "contentrating": "TV-14", "len": 734, "language": "en", "prodq": 1,
 "producer": {"name": "Blender Foundation", "domain": "blender.org"}, "kwarray": ["live-action"],
 "cids": [{"name": "example.com", "ids": ["tears-of-steel"]}],
 "title_q": "Tears%20of%20Steel", "series_q": "",
 "hdr": "v1;id=tears-of-steel;g=652,325;c=652,325;r=TV-14;l=734;la=en;pq=1",
 "updated": "2026-10-28T00:00:00Z"}
```

- Every key is always present. Absent text is `""`, absent numbers are `null`, and `producer` is `null` when the
  catalog names no producer.
- `cids` lists the title's ids by registry: the publisher's own domain with the content id first, then each
  `external_ids` registry in domain order.
- `title_q` and `series_q` are `encodeURIComponent` forms, for ad servers that take the title as a URL parameter.
- `hdr` is the compact form for a request header,
  `v1;id=<id>;g=<genres>;c=<cat>;r=<rating>;l=<seconds>;la=<language>;pq=<prodq>`, with absent elements left out and
  the rating percent-encoded.
- The schema is [`contracts/catalog-context.schema.json`](../contracts/catalog-context.schema.json), and `v1` never
  changes. MediaTailor cannot invalidate cached Function responses, so a new schema ships as `v2` under a new prefix
  with a new cache key.

## Genres and categories

Catalog genre tags become [IAB Tech Lab Content Taxonomy 3.1](https://github.com/InteractiveAdvertisingBureau/Taxonomies)
ids through IAB Tech Lab's CTV Genre Mapping, in
[`genre-map.toml`](../core/src/fast_channels/core/crawl/taxonomy/genre-map.toml):

- The `[ctv]` table is the CTV Genre Mapping: each CTV genre and its 3.1 id, for example Action → `325`, Animation →
  `641`, Comedy → `646`, Documentary → `332`, Fantasy → `335`, Sci-Fi → `652`. Ids are strings, and some are
  alphanumeric (Crime → `KHPC5A`).
- The `[tags]` table maps each lower-case catalog tag to a CTV genre: `sci-fi` → `Sci-Fi`. Add your CMS's tags here;
  loading the map fails on any id that is not in Content Taxonomy 3.1
  ([`ct31-ids.txt`](../core/src/fast_channels/core/crawl/taxonomy/ct31-ids.txt)), on a tag that names no CTV genre, and
  on a tag that is not lower case.
- A title gets at most three genres, the most apt first (the catalog's order), without repeats: `adventure` and
  `action` are both `325`. `genre` is the 3.1 name of the first one. `cat` equals `genres`, and `gtax` and `cattax`
  are always `9` (Content Taxonomy 3.1), because OpenRTB's default `cattax`, 1, means Content Taxonomy 1.0.
- Tags with no CTV genre, such as `live-action` or `trailer`, are not genres: they go to `kwarray` with the catalog's
  `keywords`.

The taxonomy files are © IAB Technology Laboratory, Inc., licensed under CC BY 3.0; the source commit, checksums and
changes are in [`ATTRIBUTION.md`](../core/src/fast_channels/core/crawl/taxonomy/ATTRIBUTION.md).

## The PRE_ADS lookup

With `modules.crawl.pre_ads` (on by default) the release bundle maps one PRE_ADS tree on every general sibling of a
monetized channel (`<channel>-p-a` and `-p-b`); the house-only probe `-p-z` has none. The Release Controller applies
it with the rest of the bundle (`<p>_pa_root_rNNNN`, `<p>_pa_meta_rNNNN`), so it is released, promoted and rolled
back like every other function.

| Function | What it does |
|---|---|
| `<p>_pa_root` | `SEQUENTIAL_EXECUTOR`, 400 ms: runs the lookup and commits one value, the `x-fast-ctx` request header |
| `<p>_pa_meta` | `HTTP_REQUEST` GET `<content base>/catalog/ctx/v1/<content id>.json`, 250 ms, skipped when the break has no UPID. The content id is `scte.segmentation_upid` with its `FST3` prefix removed if MediaTailor left it. `Cache` 300/300 s keyed `meta\|v1\|<UPID>` |

- **Load.** MediaTailor's Function cache is per playback configuration and splits into about 4 partitions that each
  coalesce concurrent misses, so the CDN sees about 4 requests per title per sibling in each 300 s window, whatever
  the audience (measured with herds of 100 and 300 sessions). A title missing from the catalog is a cached 404 too
  ([Serving and misses](#serving-and-misses)).
- **Output.** `x-fast-ctx` carries the object's `hdr` when the lookup answered 200 with a `v1` object, and is empty
  otherwise. The expression caps it at 1,000 characters, so the release validators can prove the documented output
  limit (which the service does not enforce).
- **No viewer data.** The URL, the header and the cache key read only the UPID (validator R-F5). Nothing is written to
  `player_params`, and the templated POST body is untouched: MediaTailor inserts values without JSON escaping and
  silently skips an ad request whose body is not valid JSON.
- **Budget.** The 400 ms come out of the ad request's wall clock: MediaTailor's Live (and VOD) maximum is a deadline
  that includes PRE_ADS time, and what it cuts is the ad request. The validators require each sibling's maximum to be
  at least 400 ms plus `AdsRequestTimeoutMilliseconds` (6,000 ≥ 400 + 2,000).
- **Fail open.** An unknown title, an origin error or timeout, a break without a UPID or an exhausted hook budget
  sends the ad request without the header.
- `pre_ads: false` leaves PRE_ADS unmapped; the Pod Decision Service still enriches `app.content` from the same
  objects.

## Limits

MediaTailor Functions document a limit of 1,000 characters per output value and 20,000 characters per response body.
The service does not enforce the output limit, so the context object does:

| Value | Limit |
|---|---|
| `title`, `series`, `season`, `contentrating`, producer name, each keyword | 200 characters, on one line (runs of whitespace become one space) |
| `hdr`, `title_q`, `series_q` (Function outputs) | 1,000 characters, cut at a character boundary; only an exceptionally long rating is ever cut in `hdr` |
| The whole object | 20,000 characters; the builder refuses a larger one |

## Privacy

The object, the Function's URL, headers and cache key hold only the content id and catalog metadata. The catalog
origin sees MediaTailor's server-side client, never a viewer. Content ids are written by the schedule into the
stream, so a player cannot claim to watch a different title.
