"""Guests, consent and kids on the playback path (``modules.identity``, ``modules.release``; DESIGN-v2.2 §5.5).

Runs only on a stage with the Identity, Pds and Release stacks, the demand module deployed, a child-directed channel
``<prefix>-kids`` (``modules.release.child_directed``) and the kids twins (``kids_pairs``), as ``stages/fastci.json``
and ``fastm.json`` have them; skipped otherwise. The module fixture does the billable part once:

1. create two viewers of the test's own: ``consented`` records a consent with neither choice (PSEUDONYMOUS),
   ``unconsented`` records none;
2. start the monetized channel and the kids channel, ingest the test pattern while Prebid Server scales out, wait
   until it is warm, and schedule the pattern three times on each channel (a 60 s break each);
3. once both serve, join as a player does: ``consented`` and ``unconsented`` on a ticket each, ``guest`` on a guest
   ticket, ``kids_profile`` with ``consented``'s token and ``profile: "kids"`` (the kids twin, no ticket) and
   ``kids_channel`` on the kids channel (no ticket, for anyone);
4. after the first break, ``consented`` opts out (``PUT /consent``);
5. stop both channels and read each session's ``pds.policy`` and ``pds.select`` records from the sidecar's log.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

import pytest
from _stack import Module, Plane, Stage
from harness import BREAK_POLICY, LEAD, Channel, Player, Tools
from harness_demand import DemandEdge
from harness_identity import (
    decision_records,
    guest,
    join,
    mint,
    parameter,
    playback_plan,
    record_consent,
    viewer,
    wait_until_ready,
    wait_until_serving,
)
from harness_pods import Viewer, ipv4_client, watch
from playback import Window, break_windows

from fast_channels.core.identity.names import Parameter
from fast_channels.core.identity.ticket import key
from fast_channels.core.identity.vid import viewer_id
from fast_channels.core.pds.models import OWN_BOOK
from fast_channels.core.pds.records import POLICY_RECORD, SELECT_RECORD

pytestmark = pytest.mark.e2e

PROGRAMS: Final = 3
TAIL: Final = timedelta(seconds=60)
"""How long the sessions keep playing after the last break's scheduled end: the live playlist lags the wall clock."""
CONSENT_FRESH: Final = timedelta(seconds=10)
"""The PDS keeps a consent 5 s: a decision this long after the opt-out follows it."""
LOG_TIME: Final = "%Y-%m-%d %H:%M:%S,%f%z"
"""Powertools' ``timestamp`` of a decision record."""


@dataclass
class Played:
    """One run: who joined with what, and what the PDS decided for each session's breaks."""

    stage: Stage
    vids: dict[str, str]
    """By role, the signed-in viewers' ``vid``."""
    answers: dict[str, dict[str, Any]]
    """By role, the mint's answer the session played."""
    sessions: dict[str, Viewer]
    windows: list[Window]
    opted_out_at: datetime
    records: dict[str, list[dict[str, Any]]]
    selects: dict[str, list[dict[str, Any]]]


def decided_at(record: dict[str, Any]) -> datetime:
    return datetime.strptime(record["timestamp"], LOG_TIME)


def kids_channel(stage: Stage) -> Channel:
    channels = {entry["name"]: entry for entry in stage.json_output(Plane.MEDIA, "Channels")}
    if (entry := channels.get(f"{stage.prefix}-kids")) is None:
        pytest.skip(f"stage {stage.prefix} has no child-directed channel {stage.prefix}-kids (stages/fastci.json)")
    return Channel.of(entry)


def schedule(tools: Tools, channel: str, asset_id: str) -> list[Window]:
    start = (datetime.now(UTC) + LEAD).replace(microsecond=0)
    planned = tools(
        "schedule___plan",
        channel=channel,
        picks=[asset_id] * PROGRAMS,
        start=start.isoformat(),
        break_policy=BREAK_POLICY,
    )
    assert planned["publishable"], f"plan {planned['plan_id']} has errors: {planned['violations']}"
    return break_windows(tools("schedule___publish", plan_id=planned["plan_id"])["programs"])


@pytest.fixture(scope="module")
def played(
    stage: Stage, channel: Channel, tools: Tools, run_id: str, request: pytest.FixtureRequest
) -> Iterator[Played]:
    for module in (Module.IDENTITY, Module.PDS, Module.RELEASE):
        if stage.status(module) is None:
            pytest.skip(f"stage {stage.prefix} has no {module} stack (modules.identity with the demand module)")
    edge = DemandEdge(stage.prefix, stage.session)
    if not edge.deployed:
        pytest.skip(f"stage {stage.prefix} has no demand module deployed (make deploy-demand)")
    kids = kids_channel(stage)
    kms = stage.session.client("kms")
    run = run_id.lower()
    tickets: list[str] = []
    with (
        viewer(stage, f"e2e-kids-c-{run}@personas.example.com", persona="e2e-consented") as consented,
        viewer(stage, f"e2e-kids-u-{run}@personas.example.com", persona="e2e-unconsented") as unconsented,
    ):
        record_consent(stage, consented.access_token, opt_out=False)
        vids = {
            role: viewer_id(
                kms, key_id=parameter(stage, Parameter.VID_KEY), issuer=parameter(stage, Parameter.ISSUER), sub=v.sub
            )
            for role, v in (("consented", consented), ("unconsented", unconsented))
        }
        try:
            with ipv4_client(follow_redirects=True, timeout=20.0) as http:
                try:
                    for name in (channel.name, kids.name):
                        tools("channels___start_channel", channel=name)
                    asset = request.getfixturevalue("ingested")  # while Prebid Server scales out
                    wait_until_ready(stage, edge)
                    windows = schedule(tools, channel.name, asset.asset_id)
                    windows += schedule(tools, kids.name, asset.asset_id)
                    for name in (channel.name, kids.name):
                        wait_until_serving(http, playback_plan(stage, name).playback["hls"])
                    sessions: dict[str, Viewer] = {}
                    answers: dict[str, dict[str, Any]] = {}
                    for role, signed in (("consented", consented), ("unconsented", unconsented)):
                        sessions[role], answers[role] = join(stage, http, signed.access_token, channel.name)
                    answers["guest"] = guest(stage, channel.name)
                    sessions["guest"] = Viewer.of(Player(client=http), http.get(answers["guest"]["url"]))
                    answers["kids_profile"] = mint(stage, consented.access_token, channel.name, profile="kids")
                    sessions["kids_profile"] = Viewer.join(Player(client=http), answers["kids_profile"]["url"])
                    answers["kids_channel"] = guest(stage, kids.name)
                    sessions["kids_channel"] = Viewer.join(Player(client=http), answers["kids_channel"]["url"])
                    joined = datetime.now(UTC)
                    tickets += [
                        urlsplit(a["url"]).query.removeprefix("ads.t=")
                        for a in answers.values()
                        if "ads.t=" in a["url"]
                    ]
                    first_end = min(w.ends for w in windows)
                    watch(list(sessions.values()), ends=first_end)
                    record_consent(stage, consented.access_token, opt_out=True)
                    opted_out_at = datetime.now(UTC)
                    watch(list(sessions.values()), ends=max(w.ends for w in windows) + TAIL)
                finally:
                    for name in (channel.name, kids.name):
                        tools("channels___stop_channel", channel=name)
            since = joined - timedelta(minutes=1)
            yield Played(
                stage=stage,
                vids=vids,
                answers=answers,
                sessions=sessions,
                windows=windows,
                opted_out_at=opted_out_at,
                records=decision_records(stage, sessions, since, POLICY_RECORD),
                selects=decision_records(stage, sessions, since, SELECT_RECORD),
            )
        finally:
            table = stage.session.resource("dynamodb", region_name=stage.region).Table(
                parameter(stage, Parameter.TABLE)
            )
            for ticket in tickets:
                table.delete_item(Key={"pk": key(ticket)})


def own_book_only(selects: list[dict[str, Any]]) -> bool:
    return all(bid["bidder"] == OWN_BOOK for select in selects for bid in select["bids"])


def test_a_guest_plays_contextual_on_their_own_ticket(played: Played) -> None:
    assert "ads.t=" in played.answers["guest"]["url"]
    records = played.records["guest"]
    assert records, "the guest's session had a break"
    for record in records:
        assert (record["mode"], record["reason"], record.get("vid")) == ("CONTEXTUAL", "bound", None), record
        assert record["cid"] == played.answers["guest"]["cid"], "the guest's own ticket, not an anonymous session"


def test_a_signed_in_viewer_without_a_consent_is_safe_with_no_third_party(played: Played) -> None:
    records = played.records["unconsented"]
    assert records
    for record in records:
        assert (record["mode"], record["reason"], record["bidders"]) == ("SAFE", "consent_unavailable", []), record
    assert own_book_only(played.selects["unconsented"]), played.selects["unconsented"]


def test_an_opt_out_reaches_the_viewers_next_breaks(played: Played) -> None:
    records = played.records["consented"]
    before = [r for r in records if decided_at(r) < played.opted_out_at]
    after = [r for r in records if decided_at(r) >= played.opted_out_at + CONSENT_FRESH]
    assert before and after, (played.opted_out_at, [(r["timestamp"], r["mode"]) for r in records])
    for record in before:
        assert (record["mode"], record["reason"], record["vid"]) == ("PSEUDONYMOUS", "bound", played.vids["consented"])
    for record in after:
        assert (record["mode"], record["reason"], record.get("vid")) == ("CONTEXTUAL", "opted_out", None), record


@pytest.mark.parametrize("role", ["kids_profile", "kids_channel"])
def test_a_kids_session_is_kids_with_no_third_party_bidder(played: Played, role: str) -> None:
    answer = played.answers[role]
    assert "ads.t=" not in answer["url"] and answer["expires_in"] is None, "KIDS has no viewer id: no ticket"
    records = played.records[role]
    assert records, f"the {role} session had a break"
    for record in records:
        assert (record["mode"], record["reason"], record["bidders"], record.get("vid")) == ("KIDS", "kids", [], None)
    configurations = {r["pc"] for r in records}
    if role == "kids_profile":
        assert configurations <= {f"{played.stage.prefix}-demo-p-k", f"{played.stage.prefix}-demo-p-l"}, configurations
    else:
        assert all(pc.startswith(f"{played.stage.prefix}-kids-p-") for pc in configurations), configurations
    selects = played.selects[role]
    assert own_book_only(selects), selects
    in_pods = [bid for select in selects for bid in select["bids"] if bid["ref"] in select["pod"]]
    assert all(bid["house"] for bid in in_pods), "a KIDS pod is house ads (no creative is approved for children)"


def test_every_session_got_a_pod(played: Played) -> None:
    """Consent and kids decide who may bid, never whether a break is filled: house ads fill it."""
    for role, selects in played.selects.items():
        assert selects and all(s.get("pod") and s["filled_ms"] > 0 for s in selects), (role, selects)
