"""Show-level content metadata for ad requests (the ``crawl`` module): catalog context objects and their taxonomy.

Each ad break carries its title's content id in the SCTE-35 UPID (:func:`fast_channels.core.timeline.upid`). The
catalog context object of that id holds the title's show-level metadata (title, series, genres in IAB Tech Lab Content
Taxonomy 3.1, rating, length, language, producer) and nothing about any viewer.
"""
