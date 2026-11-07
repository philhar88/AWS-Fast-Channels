"""Viewer identity on the playback path (ADR-0018): a signed-in viewer's session reaches the Pod Decision Service's
decision record by their ``vid``; a replayed ticket, an expired one and a guest stay anonymous.

Runs only on a stage with the Identity, Pds and Release stacks and the demand module deployed (``STAGE=fastci`` or
``fastm`` after ``make deploy-demand``); skipped otherwise. The module fixture does the billable part once:

1. create a viewer of the test's own in the pool and sign it in;
2. start the stage's first monetized channel, ingest the test pattern while Prebid Server scales out, wait until it
   is warm, and schedule the pattern with one 60.06 s break;
3. wait until the channel serves, mint the late ticket, then join through the CDN as a player does: the signed-in
   viewer with a fresh ticket per attempt (``harness_identity.join``), and the late ticket is put past its redeem
   window; once the first session is bound, a second session with the same ticket (a replay), a guest on the plan's
   own URL, and the late ticket, one request each. All of them watch the break;
4. stop the channel, and read each session's ``pds.policy`` and ``pds.select`` records from the sidecar's log.

The ticket's row then says which session it is bound to, and the records say what every break of every session was.
"""

import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from _stack import Module, Stage
from harness import BREAK_POLICY, LEAD, Channel, Player, Tools
from harness_demand import DemandEdge
from harness_identity import (
    SignedIn,
    decision_records,
    join,
    mint,
    parameter,
    playback_plan,
    record_consent,
    ticket_of,
    viewer,
    wait_until_ready,
    wait_until_serving,
)
from harness_pods import Viewer, ipv4_client, watch
from playback import Window, break_windows

from fast_channels.core.identity.names import Parameter
from fast_channels.core.identity.ticket import key
from fast_channels.core.identity.vid import viewer_id
from fast_channels.core.pds.records import POLICY_RECORD, SELECT_RECORD

pytestmark = pytest.mark.e2e

PERSONA: Final = "e2e-session"
TAIL: Final = timedelta(seconds=60)
"""How long the sessions keep playing after the break's scheduled end: the live playlist lags the wall clock."""
REPLAY_AFTER: Final = timedelta(seconds=10)
"""How long after the signed-in viewer joined the replay joins: the first session's PRE_SESSION redeem has landed."""


@dataclass
class Played:
    """One run: who joined with what, and what the PDS decided for each session's breaks."""

    stage: Stage
    signed_in: SignedIn
    vid: str
    ticket: str
    cid: str
    configuration: str
    """The stable sibling the playback plan names, which the ticket was minted for."""
    sessions: dict[str, Viewer]
    """By role: ``signed_in``, ``replay``, ``guest``, ``late``."""
    windows: list[Window]
    records: dict[str, list[dict[str, Any]]]
    """Each role's ``pds.policy`` records, as logged: Powertools leaves out a key whose value is null (``vid``, ``cid``
    of an anonymous break)."""
    selects: dict[str, list[dict[str, Any]]]
    """Each role's ``pds.select`` records."""

    def row(self) -> dict[str, Any]:
        table = self.stage.session.resource("dynamodb", region_name=self.stage.region).Table(
            parameter(self.stage, Parameter.TABLE)
        )
        item = table.get_item(Key={"pk": key(self.ticket)}, ConsistentRead=True).get("Item")
        assert item is not None
        return dict(item)


@pytest.fixture(scope="module")
def stage_with_identity(stage: Stage) -> Stage:
    for module in (Module.IDENTITY, Module.PDS, Module.RELEASE):
        if stage.status(module) is None:
            pytest.skip(f"stage {stage.prefix} has no {module} stack (modules.identity with the demand module)")
    return stage


def expire(stage: Stage, ticket: str) -> None:
    """Put ``ticket`` past its redeem window, as if its player opened it too late."""
    table = stage.session.resource("dynamodb", region_name=stage.region).Table(parameter(stage, Parameter.TABLE))
    table.update_item(
        Key={"pk": key(ticket)},
        UpdateExpression="SET redeem_by = :past",
        ExpressionAttributeValues={":past": int(time.time() * 1000) - 1_000},
    )


@pytest.fixture(scope="module")
def played(
    stage_with_identity: Stage, channel: Channel, tools: Tools, run_id: str, request: pytest.FixtureRequest
) -> Iterator[Played]:
    stage, edge = stage_with_identity, DemandEdge(stage_with_identity.prefix, stage_with_identity.session)
    if not edge.deployed:
        pytest.skip(f"stage {stage.prefix} has no demand module deployed (make deploy-demand)")
    kms = stage.session.client("kms")
    with viewer(stage, f"e2e-session-{run_id.lower()}@personas.example.com", persona=PERSONA) as signed_in:
        record_consent(stage, signed_in.access_token, opt_out=False)  # PSEUDONYMOUS: no consent is SAFE
        vid = viewer_id(
            kms,
            key_id=parameter(stage, Parameter.VID_KEY),
            issuer=parameter(stage, Parameter.ISSUER),
            sub=signed_in.sub,
        )
        tickets: list[str] = []
        try:
            with ipv4_client(follow_redirects=True, timeout=20.0) as http:
                try:
                    tools("channels___start_channel", channel=channel.name)
                    asset = request.getfixturevalue("ingested")  # while Prebid Server scales out
                    wait_until_ready(stage, edge)
                    start = (datetime.now(UTC) + LEAD).replace(microsecond=0)
                    planned = tools(
                        "schedule___plan",
                        channel=channel.name,
                        picks=[asset.asset_id],
                        start=start.isoformat(),
                        break_policy=BREAK_POLICY,
                    )
                    assert planned["publishable"], f"plan {planned['plan_id']} has errors: {planned['violations']}"
                    windows = break_windows(tools("schedule___publish", plan_id=planned["plan_id"])["programs"])
                    plan = playback_plan(stage, channel.name)
                    wait_until_serving(http, plan.playback["hls"])  # a just-started channel answers 404 for a while
                    late = mint(stage, signed_in.access_token, channel.name)
                    joined = datetime.now(UTC)
                    signed, answer = join(stage, http, signed_in.access_token, channel.name)
                    tickets += [ticket_of(answer["url"]), ticket_of(late["url"])]
                    expire(stage, tickets[1])
                    sessions = {"signed_in": signed}
                    watch(list(sessions.values()), ends=datetime.now(UTC) + REPLAY_AFTER)
                    # The same ticket again, and the expired one: one request each, never retried.
                    sessions["replay"] = Viewer.of(Player(client=http), http.get(answer["url"]))
                    sessions["guest"] = Viewer.join(Player(client=http), plan.playback["hls"])
                    sessions["late"] = Viewer.of(Player(client=http), http.get(late["url"]))
                    watch(list(sessions.values()), ends=windows[-1].ends + TAIL)
                finally:
                    tools("channels___stop_channel", channel=channel.name)
            yield Played(
                stage=stage,
                signed_in=signed_in,
                vid=vid,
                ticket=tickets[0],
                cid=answer["cid"],
                configuration=plan.configuration,
                sessions=sessions,
                windows=windows,
                records=decision_records(stage, sessions, joined - timedelta(minutes=1), POLICY_RECORD),
                selects=decision_records(stage, sessions, joined - timedelta(minutes=1), SELECT_RECORD),
            )
        finally:
            table = stage.session.resource("dynamodb", region_name=stage.region).Table(
                parameter(stage, Parameter.TABLE)
            )
            for ticket in tickets:
                table.delete_item(Key={"pk": key(ticket)})


def test_the_signed_in_viewers_breaks_are_theirs_by_vid(played: Played) -> None:
    records = played.records["signed_in"]
    assert records, "the signed-in session had a break"
    for record in records:
        assert (record["reason"], record["mode"]) == ("bound", "PSEUDONYMOUS"), record
        assert (record["vid"], record["cid"]) == (played.vid, played.cid), record
    assert played.signed_in.sub not in str(records), "the raw sub never reaches a record"


def test_the_ticket_is_bound_to_the_first_session_only(played: Played) -> None:
    row = played.row()
    assert (row["redeemedBy"], row["pc"]) == (played.sessions["signed_in"].sid, played.configuration)
    assert int(row["bound_at"]) <= int(row["last_seen"])


def test_a_replayed_ticket_is_refused(played: Played) -> None:
    records = played.records["replay"]
    assert records and all((r["reason"], r["mode"], r.get("vid")) == ("replay", "CONTEXTUAL", None) for r in records)


def test_a_guest_and_a_late_ticket_are_contextual(played: Played) -> None:
    for role, reason in (("guest", "missing"), ("late", "expired_or_raced")):
        records = played.records[role]
        assert records and all(
            (r["reason"], r["mode"], r.get("vid")) == (reason, "CONTEXTUAL", None) for r in records
        ), (
            role,
            records,
        )


def test_every_session_got_a_pod(played: Played) -> None:
    """Anonymous sessions still get a pod: identity never costs a viewer the break. (Whether its ads are stitched at
    their first sight is MediaTailor's creative import, ``test_14``'s concern.)"""
    for role, selects in played.selects.items():
        assert selects and all(s.get("pod") and s["filled_ms"] > 0 for s in selects), (role, selects)
