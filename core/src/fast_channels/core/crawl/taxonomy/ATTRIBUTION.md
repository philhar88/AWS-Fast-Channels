# IAB Tech Lab Content Taxonomy 3.1 and CTV Genre Mapping

`ct31-ids.txt` and the `[ctv]` table of `genre-map.toml` are derived from the IAB Tech Lab taxonomies, © IAB
Technology Laboratory, Inc., licensed under the
[Creative Commons Attribution 3.0 License](https://creativecommons.org/licenses/by/3.0/).

## Source

Repository [InteractiveAdvertisingBureau/Taxonomies](https://github.com/InteractiveAdvertisingBureau/Taxonomies) at
commit `0fa1a8b3d7b6a94d245ee858d936dcfe021ca2c5` (2026-10-27):

| File | SHA-256 | Used for |
|---|---|---|
| `Content Taxonomies/Content Taxonomy 3.1.tsv` | `7212cdc496ba347a03e703b1932bdcdd4fd29089b058f4edeb4d3da1f1222ea7` | The categories in `ct31-ids.txt` (704 ids) |
| `Content Taxonomies/Content Taxonomy 3.0 Descriptive Vectors.tsv` | `fc94f5f880964f10c75c096e1dc724a8bea07c40151e1cc3e4189fab39225d4a` | The descriptive vectors in `ct31-ids.txt` (47 ids) |
| `Taxonomy Mappings/CTV Genre Mapping.tsv` | `17876eac9d1db201f63cebe918560ab54a1af9ec374eb34a73665d062674de4f` | The `[ctv]` table of `genre-map.toml` (44 CTV genres) |

The repository has no licence file. Its README states that the material is licensed under the Creative Commons
Attribution 3.0 License.

## Changes

- `ct31-ids.txt` keeps two columns of each TSV file, the unique id and the name, in the files' order.
- The `[ctv]` table keeps the CTV Genre Mapping's first two columns, the CTV genre and its Content Taxonomy 3.1 unique
  id. Each comment gives the id's name from the Content Taxonomy 3.1 file instead of the mapping's third column (the
  mapping labels 244 "Parnormal Phenomenons", while the taxonomy names it "Paranormal Phenomena").
- The `[tags]` table, the mapping from catalog genre tags to CTV genres, is ours.

## Why the descriptive vectors are included

The Content Taxonomy 2.x and 3.x have two parts: categories that say what the content is about, and orthogonal
attributes ("vectors") such as content purpose, form factor and source, all identified by ids from the same space
(the repository's `implementation.md`, "Descriptions of the Vectors"). Content Taxonomy 3.1 ships no vector file of its
own and keeps the 3.0 vectors. The CTV Genre Mapping maps News to 1020, which is the vector Content Purpose >
Informational > News, so a list of the 3.1 category ids alone would reject a genre that IAB Tech Lab maps.

## Regenerate

From a checkout of the repository at a new commit, with `IDS` set to this directory's `ct31-ids.txt`:

```bash
rows() { tr -d '\r' < "$1" | awk -F'\t' 'NR > 2 && $1 != "" {print $1 "\t" $3}'; }
{
  head -3 "$IDS"   # keep the three header lines
  rows "Content Taxonomies/Content Taxonomy 3.1.tsv"
  echo "# Descriptive vectors (Content Taxonomies/Content Taxonomy 3.0 Descriptive Vectors.tsv)"
  rows "Content Taxonomies/Content Taxonomy 3.0 Descriptive Vectors.tsv"
} > ids.new && mv ids.new "$IDS"
```

Run at the commit above, this reproduces `ct31-ids.txt` byte for byte. Update the commit and the checksums here, and
compare the `[ctv]` table with the new `CTV Genre Mapping.tsv`; `uv run pytest core/tests` then checks every id that
`genre-map.toml` uses against the new list.
