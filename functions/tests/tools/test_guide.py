"""Guide entries from PROGRAM records: the breaks' event ids and UPIDs only with the ops module (EPG_BREAK_IDS)."""

import pytest

from fast_channels.core.timeline import PlannedBreak
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import _guide
from fast_channels.functions.tools import library_store as store
from fast_channels.functions.tools.library_store import ProgramStatus


def program() -> store.ProgramRecord:
    return store.ProgramRecord(
        channel="fast-demo",
        status=ProgramStatus.PUBLISHED,
        name="20261029T010143-elephants-dream",
        asset_id="elephants-dream",
        vod_source="elephants-dream-standard",
        content_id="elephants-dream",
        start_ms=1_793_235_703_000,
        duration_ms=713_000,
        ladder_version="standard@843ed1d05a",
        breaks=[PlannedBreak(offset_ms=217_984, duration_ms=60_060, slate_gops=30, event_id=1820521412, upid="FST3ed")],
    )


@pytest.mark.parametrize(("flag", "ids"), [(None, False), ("false", False), ("true", True)])
def test_guide_breaks_carry_ids_only_with_the_flag(
    monkeypatch: pytest.MonkeyPatch, flag: str | None, ids: bool
) -> None:
    if flag is None:
        monkeypatch.delenv("EPG_BREAK_IDS", raising=False)
    else:
        monkeypatch.setenv("EPG_BREAK_IDS", flag)
    settings.cache_clear()

    [entry] = _guide.guide_entry(program()).breaks

    assert (entry.event_id, entry.upid) == ((1820521412, "FST3ed") if ids else (None, None))
    assert (entry.offset_ms, entry.duration_ms) == (217_984, 60_060)
    settings.cache_clear()
