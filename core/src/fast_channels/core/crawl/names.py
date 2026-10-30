"""Names the crawl module shares with the Pod Decision Service, its decision records, the lake and the e2e suite."""

from enum import StrEnum
from typing import Final

CONTRIBUTOR: Final = "crawl"
"""The crawl module's patch contributor: ``pds.policy`` records list it in ``contributors``."""
RECORD_ARM: Final = "enr"
"""``contributed.enr`` of a ``pds.policy`` record: 1 for the enriched arm, 0 for the ``crawl-v1`` holdout. The same
value rides in our impression tracker (``enr=``), so delivery records carry the arm too."""
RECORD_SOURCE: Final = "ctx"
"""``contributed.ctx`` of a ``pds.policy`` record: where the break's metadata was found (:class:`Source`), in either
arm; only the enriched arm's patch carries it."""
KPI_VIEW: Final = "v_crawl_kpis"
"""The lake view of the enrichment KPIs (coverage, bid rate, fill and price by arm)."""


class Source(StrEnum):
    """Where a break's metadata was found."""

    SNAPSHOT = "snapshot"
    """The PDS's copy of the title's catalog context object."""
    HEADER = "header"
    """The ``x-fast-ctx`` header of the PRE_ADS lookup, when the PDS's copy lacks the title (publish lag)."""
    NONE = "none"
    """Nowhere: a break without a UPID, or a title in neither."""
