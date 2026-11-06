"""One session's ad decisions, as the viewer app's demo panel shows them: each break's privacy mode, the viewer's tier,
the ad-load policy the Pod Decision Service applied, and the pod that aired.

The input is the session's decision records (``pds.policy`` and ``pds.select``, :mod:`fast_channels.core.pds.records`)
as CloudWatch Logs Insights returns them: one JSON log line each. Records are joined on ``decision_id``
(``<sid>.<evid>``); a break with only one of its two records still shows what it has. Probe sessions' records are
left out. Pure functions, so the panel's rules are tested without AWS.
"""

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

SESSION_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
"""A MediaTailor session id (a lowercase UUID); nothing else reaches the query."""
POLICY: Final = "pds.policy"
SELECT: Final = "pds.select"
NOT_COUNTED: Final = frozenset({"won", "standalone"})
"""Outcomes left out of the "not run" counts."""


class AdLoad(BaseModel):
    """``pds.select`` ``ad_load``: the tier's policy as applied to the break."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    policy: str
    reason: str
    max_ads: int | None
    bar_cpm: float
    slots: int


class PodAd(BaseModel):
    """One ad of the pod, in sequence order."""

    model_config = ConfigDict(frozen=True)

    bidder: str
    price_cpm: float
    house: bool
    deal: str | None
    seconds: float | None


class BreakDecision(BaseModel):
    """One break of the session (console/src/viewer/adPanel.ts ``BreakDecision``)."""

    model_config = ConfigDict(frozen=True)

    decision_id: str
    evid: str
    at: str
    mode: str | None
    mode_reason: str | None
    tier: str | None
    ad_load: AdLoad | None
    pod_reason: str | None
    ads: list[PodAd]
    filled_s: float | None
    break_s: float | None
    dropped: dict[str, int]


class SessionDecisions(BaseModel):
    """``GET /sessions/{sid}/breaks``: the session's breaks, latest first."""

    model_config = ConfigDict(frozen=True)

    sid: str
    breaks: list[BreakDecision]


def insights_query(sid: str) -> str:
    """The Logs Insights query of one session's decision records, latest first.

    Raises:
        ValueError: ``sid`` is not a MediaTailor session id.
    """
    if not SESSION_ID.fullmatch(sid):
        raise ValueError("not a MediaTailor session id")
    return (
        "fields @timestamp, @message"
        f' | filter sid = "{sid}" and (record = "{POLICY}" or record = "{SELECT}")'
        " | sort @timestamp desc | limit 400"
    )


def parse_results(rows: Iterable[Iterable[Mapping[str, str]]]) -> list[dict[str, Any]]:
    """The JSON records of ``GetQueryResults`` rows (``[{field, value}, ...]``), each with its ``@timestamp``."""
    records = []
    for row in rows:
        fields = {cell["field"]: cell["value"] for cell in row}
        try:
            record = json.loads(fields.get("@message", ""))
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            record.setdefault("@timestamp", fields.get("@timestamp"))
            records.append(record)
    return records


def viewer_ids(records: Iterable[Mapping[str, Any]]) -> set[str]:
    """The viewer ids (``vid``) that the session's ``pds.policy`` records name."""
    return {str(r["vid"]) for r in records if r.get("record") == POLICY and isinstance(r.get("vid"), str)}


def _at(record: Mapping[str, Any]) -> str:
    """When the record was logged, ISO 8601 in UTC, from Logs Insights' ``@timestamp`` (``2026-11-05 20:04:01.123``,
    UTC); empty when the row has none."""
    raw = record.get("@timestamp")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=UTC).isoformat(timespec="milliseconds")
    except ValueError:
        return raw


def _latest(records: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return max(records, key=_at, default=None)


def _text(record: Mapping[str, Any] | None, field: str) -> str | None:
    value = record.get(field) if record else None
    return str(value) if isinstance(value, str) and value else None


def _seconds(ms: object) -> float | None:
    return round(ms / 1000, 3) if isinstance(ms, int | float) else None


def _pod(select: Mapping[str, Any]) -> tuple[list[PodAd], dict[str, int]]:
    bids = {str(b.get("ref")): b for b in select.get("bids", []) if isinstance(b, dict)}
    ads = [
        PodAd(
            bidder=str(bid.get("bidder", "")),
            price_cpm=float(bid.get("price") or 0),
            house=bool(bid.get("house")),
            deal=bid.get("dealid") if isinstance(bid.get("dealid"), str) else None,
            seconds=_seconds(bid.get("vast_dur_ms")),
        )
        for ref in select.get("pod", [])
        if (bid := bids.get(str(ref))) is not None
    ]
    dropped = Counter(str(b.get("outcome")) for b in bids.values() if str(b.get("outcome")) not in NOT_COUNTED)
    below_floor = select.get("below_floor")
    if isinstance(below_floor, int) and below_floor > 0:
        dropped["below_floor"] += below_floor
    return ads, dict(sorted(dropped.items()))


def summarize(sid: str, records: Iterable[Mapping[str, Any]]) -> SessionDecisions:
    """The session's breaks from its decision records, latest first; probe records are left out."""
    by_break: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    for record in records:
        if record.get("sid") != sid or record.get("probe") is True or not isinstance(record.get("decision_id"), str):
            continue
        kind = record.get("record")
        if kind in {POLICY, SELECT}:
            by_break.setdefault(record["decision_id"], {POLICY: [], SELECT: []})[str(kind)].append(record)
    breaks = []
    for decision_id, kinds in by_break.items():
        policy, select = _latest(kinds[POLICY]), _latest(kinds[SELECT])
        first = select or policy or {}
        ads, dropped = _pod(select) if select else ([], {})
        raw_load = select.get("ad_load") if select else None
        breaks.append(
            BreakDecision(
                decision_id=decision_id,
                evid=str(first.get("evid", decision_id.rpartition(".")[2])),
                at=_at(first),
                mode=_text(policy, "mode") or _text(select, "mode"),
                mode_reason=_text(policy, "reason"),
                tier=_text(select, "tier"),
                ad_load=AdLoad.model_validate(raw_load) if isinstance(raw_load, dict) else None,
                pod_reason=_text(select, "reason"),
                ads=ads,
                filled_s=_seconds(select.get("filled_ms")) if select else None,
                break_s=_seconds(first.get("dur_ms")),
                dropped=dropped,
            )
        )
    breaks.sort(key=lambda b: b.at, reverse=True)
    return SessionDecisions(sid=sid, breaks=breaks)
