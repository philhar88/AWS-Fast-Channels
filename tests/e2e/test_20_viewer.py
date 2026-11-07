"""The reference viewer's API calls (``modules.viewer``, slice B3): two personas sign in, each mints a playback ticket
for the same monetized channel and plays it, and the demo's ad panel shows each of them their own breaks' tier and
ad load, which differ.

Runs only on a stage with the Viewer, Identity, Pds and Release stacks (``modules.viewer`` with ``ad_panel``, after
``make deploy-demand``, for example ``STAGE=fastci``) and the demo users seeded (``scripts/seed_personas.py``);
skipped otherwise. The module fixture does the billable part once:

1. read the viewer's site and its ``config.json`` through its distribution, as the browser does;
2. sign Dana (the ``light`` tier) and Marcus (``standard``) in with the viewer pool's public client
   (``USER_PASSWORD_AUTH``, the app's flow), with the demo users' passwords from Secrets Manager;
3. start the channel, ingest the test pattern while Prebid Server scales out, wait for its warm-up, and schedule the
   pattern twice with one 60.06 s break each;
4. ``POST /playback`` once per persona with their access token, and a guest's session on the channel's playback
   plan; play all three through both breaks, requesting every stitched ad segment, as a player does;
5. stop the channel, then ask the ad panel API for each session with each token until the breaks are decided.

What the viewers saw is read from their playlists (ad and slate seconds per break); what the PDS decided, from the
panel's answers. The browser walk-through of the same flow is docs/viewer.md's demo script.
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Final

import httpx
import pytest
from _stack import Module, Stage
from botocore.exceptions import ClientError
from harness import BREAK_POLICY, LEAD, Channel, Player, Tools, wait_for
from harness_demand import DemandEdge
from harness_identity import claims, credentials, sign_in
from harness_pods import (
    SMART_TV_USER_AGENT,
    Break,
    Viewer,
    gap_sessions_allowed,
    gapped_sessions,
    ipv4_client,
    read_unserved_prefetches,
    retrieved_for,
    wait_until_ready,
    watch,
)
from playback import Window, break_windows

from fast_channels.core.monetization.release import PlaybackPlan
from fast_channels.core.personas import PERSONAS

pytestmark = pytest.mark.e2e

LIGHT: Final = "dana"
STANDARD: Final = "marcus"
PROGRAMS: Final = 2
"""The pattern twice, one 60.06 s break each (two 30 s slots)."""
BREAK_SECONDS: Final = 60.06
JOIN_ATTEMPTS: Final = 6
"""Tickets tried per persona until a session starts (a starting channel answers 404 or 504 for a while)."""
SERVING_TIMEOUT_S: Final = 300
PANEL_TIMEOUT_S: Final = 300
"""Decision records reach Logs Insights within seconds; the panel keeps a session's records 15 s."""
TAIL: Final = timedelta(seconds=90)
"""How long the viewers keep playing after the last break's scheduled end: the live playlist lags the wall clock."""
SEGMENT_SLACK_S: Final = 6.5
"""One content segment: a break's ads and slate are counted in whole segments."""


@dataclass
class Signed:
    """A persona signed in with the viewer pool's public client."""

    persona: str
    access_token: str
    claims: dict[str, Any]


@dataclass
class Run:
    """What the module fixture played and what the panel answered."""

    site: httpx.Response
    config: dict[str, Any]
    signed: dict[str, Signed]
    tickets: dict[str, dict[str, Any]]
    """``POST /playback`` answers by persona."""
    viewers: dict[str, Viewer]
    """By persona, and ``guest`` on the playback plan."""
    windows: list[Window]
    panel: dict[str, dict[str, Any]]
    """The panel's answer for each persona's own session."""
    refused: dict[str, int]
    """HTTP status of the panel for sessions that are not the caller's: ``other`` (another persona's) and ``guest``."""
    unserved: dict[tuple[str, str], Break] = field(default_factory=dict)
    """W-2 1.33's known gaps among the sessions' breaks (``harness_pods.unserved_prefetches``)."""

    def unserved_in(self, persona: str, window: Window) -> Break | None:
        """The persona's break of ``window`` if MediaTailor left it unserved (W-2 1.33), else None."""
        sid = self.viewers[persona].sid
        return next((b for (s, _), b in self.unserved.items() if s == sid and retrieved_for(b, window)), None)


@pytest.fixture(scope="module")
def viewer_stage(stage: Stage) -> Stage:
    for module in (Module.VIEWER, Module.IDENTITY, Module.PDS, Module.RELEASE):
        if stage.status(module) is None:
            pytest.skip(f"stage {stage.prefix} has no {module} stack (modules.viewer with ad_panel, deploy-demand)")
    return stage


def signed_in(stage: Stage, persona: str) -> Signed:
    """The persona signed in as the app signs in (harness_identity: USER_PASSWORD_AUTH on the viewer app client)."""
    try:
        credentials(stage, persona)
    except LookupError, ClientError:
        pytest.skip(f"{persona} is not seeded on {stage.prefix}: run scripts/seed_personas.py --prefix {stage.prefix}")
    token = sign_in(stage, persona)
    return Signed(persona, token, claims(token))


def wait_until_serving(http: httpx.Client, url: str) -> None:
    """Until the channel's anonymous URL (the playback plan's) answers its multivariant playlist."""
    wait_for(
        lambda: http.get(url).status_code == httpx.codes.OK or None,
        timeout=SERVING_TIMEOUT_S,
        interval=10,
        what=f"{url} to serve",
    )


def schedule(tools: Tools, channel: str, asset_id: str) -> list[dict[str, Any]]:
    start = (datetime.now(UTC) + LEAD).replace(microsecond=0)
    plan = tools(
        "schedule___plan",
        channel=channel,
        picks=[asset_id] * PROGRAMS,
        start=start.isoformat(),
        break_policy=BREAK_POLICY,
    )
    assert plan["publishable"], f"plan {plan['plan_id']} has errors: {plan['violations']}"
    programs: list[dict[str, Any]] = tools("schedule___publish", plan_id=plan["plan_id"])["programs"]
    return programs


def mint(http: httpx.Client, playback_url: str, signed: Signed, channel: str) -> dict[str, Any]:
    """``POST /playback`` as the viewer app calls it: from another origin, with the access token."""
    response = http.post(
        playback_url,
        json={"channel": channel},
        headers={"Authorization": f"Bearer {signed.access_token}", "Origin": "https://viewer.example"},
    )
    assert response.status_code == 200, (signed.persona, response.status_code, response.text[:300])
    assert response.headers.get("access-control-allow-origin") == "*"
    ticket: dict[str, Any] = response.json()
    return ticket


def join(http: httpx.Client, playback_url: str, signed: Signed, channel: str) -> tuple[dict[str, Any], Viewer]:
    """A session as the viewer app starts one: a new ticket for each attempt, never a ticketed URL loaded twice.
    MediaTailor creates the session, and its PRE_SESSION redeem binds the ticket, even when it answers an error, so a
    second load of the same URL would be a replay (an anonymous session)."""
    failures = []
    for _ in range(JOIN_ATTEMPTS):
        ticket = mint(http, playback_url, signed, channel)
        response = http.get(ticket["url"])
        if response.status_code == httpx.codes.OK:
            return ticket, Viewer.of(Player(client=http), response)
        failures.append(response.status_code)
        time.sleep(5)
    raise AssertionError(f"{signed.persona}: no session after {JOIN_ATTEMPTS} tickets: {failures}")


def decisions(http: httpx.Client, panel: str, signed: Signed, sid: str) -> httpx.Response:
    return http.get(
        f"{panel.rstrip('/')}/sessions/{sid}/breaks", headers={"Authorization": f"Bearer {signed.access_token}"}
    )


@pytest.fixture(scope="module")
def run(viewer_stage: Stage, channel: Channel, tools: Tools, request: pytest.FixtureRequest) -> Run:
    stage = viewer_stage
    site_url = stage.output(Module.VIEWER, "ViewerUrl")
    panel_url = stage.output(Module.VIEWER, "AdPanelUrl")
    with ipv4_client(follow_redirects=True, timeout=20.0, headers={"User-Agent": SMART_TV_USER_AGENT}) as http:
        site = http.get(site_url)
        config = http.get(f"{site_url.rstrip('/')}/config.json").json()
        playback_url = config["playbackUrl"]
        signed = {persona: signed_in(stage, persona) for persona in (LIGHT, STANDARD)}
        try:
            tools("channels___start_channel", channel=channel.name)
            asset = request.getfixturevalue("ingested")  # while Prebid Server scales out
            wait_until_ready(stage, DemandEdge(stage.prefix, stage.session))
            windows = break_windows(schedule(tools, channel.name, asset.asset_id))
            plan = PlaybackPlan.model_validate(http.get(f"{config['planBaseUrl']}/{channel.name}.json").json())
            wait_until_serving(http, plan.playback["hls"])
            started = datetime.now(UTC)
            joined = {persona: join(http, playback_url, s, channel.name) for persona, s in signed.items()}
            tickets = {persona: ticket for persona, (ticket, _) in joined.items()}
            viewers = {persona: viewer for persona, (_, viewer) in joined.items()}
            viewers["guest"] = Viewer.join(Player(client=http), plan.playback["hls"])
            watch(list(viewers.values()), ends=windows[-1].ends + TAIL)
        finally:
            tools("channels___stop_channel", channel=channel.name)

        def answered(persona: str) -> dict[str, Any] | None:
            response = decisions(http, panel_url, signed[persona], viewers[persona].sid)
            if response.status_code != 200:
                return None
            body: dict[str, Any] = response.json()
            return body if len(body["breaks"]) >= len(windows) else None

        panel = {
            persona: wait_for(
                partial(answered, persona), timeout=PANEL_TIMEOUT_S, interval=20, what=f"{persona}'s breaks"
            )
            for persona in signed
        }
        refused = {
            "other": decisions(http, panel_url, signed[LIGHT], viewers[STANDARD].sid).status_code,
            "guest": decisions(http, panel_url, signed[LIGHT], viewers["guest"].sid).status_code,
        }
    sids = {viewer.sid for viewer in viewers.values()}
    unserved = read_unserved_prefetches(stage, sids, started - timedelta(minutes=1))
    result = Run(site, config, signed, tickets, viewers, windows, panel, refused, unserved)
    for persona, answer in panel.items():
        for found in answer["breaks"]:
            print(
                persona,
                found["evid"],
                found["mode"],
                found["tier"],
                found["ad_load"],
                [a["price_cpm"] for a in found["ads"]],
            )
    return result


def test_the_site_serves_the_app_under_its_csp_and_a_config_without_tiers(run: Run) -> None:
    assert run.site.status_code == 200
    assert "text/html" in run.site.headers["content-type"]
    csp = run.site.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert run.config["playbackUrl"].split("/")[2] in csp
    assert run.config["adPanelUrl"].split("/")[2] in csp
    personas = {p["id"]: p for p in run.config["personas"]}
    assert set(personas) == {p.id for p in PERSONAS}
    assert all("tier" not in p and "value" not in p for p in personas.values())
    assert [c["monetized"] for c in run.config["channels"]] == [True]


def test_each_persona_mints_a_ticket_with_an_access_token_of_the_playback_scope(run: Run) -> None:
    for persona, signed in run.signed.items():
        assert "fast/playback" in str(signed.claims.get("scope", "")).split(), (persona, signed.claims.get("scope"))
        assert signed.claims.get("token_use") == "access"
        ticket = run.tickets[persona]
        assert "ads.t=" in ticket["url"], ticket
        assert ticket["cid"]


def test_both_personas_play_the_same_channel_on_their_own_ticket(run: Run) -> None:
    configurations = {viewer.variant.split("/")[6] for viewer in run.viewers.values()}
    assert len({v.sid for v in run.viewers.values()}) == 3
    assert len(configurations) <= 2, configurations  # the stable sibling (and its candidate during a rollout)
    for persona, viewer in run.viewers.items():
        assert not viewer.errors, (persona, viewer.errors[:5])


def test_the_light_tier_gets_one_ad_and_slate_and_the_standard_tier_the_full_pod(run: Run) -> None:
    light, standard = run.panel[LIGHT]["breaks"], run.panel[STANDARD]["breaks"]
    assert {b["tier"] for b in light} == {"light"}
    assert {b["tier"] for b in standard} == {"standard"}
    assert all(b["mode"] in {"FULL", "PSEUDONYMOUS"} for b in light + standard)
    assert all(b["ad_load"]["policy"] == "light" and b["ad_load"]["max_ads"] == 1 for b in light)
    assert all(len(b["ads"]) <= 1 for b in light)
    assert all(len(b["ads"]) == 2 for b in standard)
    assert all(
        a["price_cpm"] > b["ad_load"]["bar_cpm"] for b in light if b["ad_load"]["reason"] == "tier" for a in b["ads"]
    )
    # Dana's breaks leave half the break to the slate; Marcus's are full.
    assert all(b["filled_s"] <= 30.03 < b["break_s"] for b in light), [(b["filled_s"], b["break_s"]) for b in light]
    assert all(b["filled_s"] >= 60.0 for b in standard), [b["filled_s"] for b in standard]
    # What the playlists stitched: at most one 30 s ad for Dana at every break, and Marcus's full pod at the last one
    # (a creative MediaTailor sees first at a real-time break airs its slate, test_14's first sight). Dana's other
    # seconds are printed, not asserted: the slate is the decision's unfilled time above (on fastcid, 2026-11-06,
    # Channel Assembly's 30 s break slate followed her ad).
    for window in run.windows:
        light_ads, other = run.viewers[LIGHT].break_seconds(window.starts, BREAK_SECONDS)
        print(f"dana's break at {window.starts:%H:%M:%S}: {light_ads} s of ads, {other} s of other segments")
        if (gap := run.unserved_in(LIGHT, window)) is not None:  # W-2 1.33: test_the_unserved_breaks_are_rare
            print(f"  skipped: MediaTailor left break {gap.evid} unserved (W-2 1.33)")
            continue
        assert 0 < light_ads <= 30.03 + SEGMENT_SLACK_S, light_ads
    last = run.windows[-1]
    standard_ads, _ = run.viewers[STANDARD].break_seconds(last.starts, BREAK_SECONDS)
    if (gap := run.unserved_in(STANDARD, last)) is not None:
        print(f"marcus's last break skipped: MediaTailor left break {gap.evid} unserved (W-2 1.33)")
    else:
        assert standard_ads >= 60.06 - SEGMENT_SLACK_S, standard_ads


# OPTIMIZATION(aws): W-2 1.33 drop this tolerance when MediaTailor serves every prefetched break
def test_the_unserved_breaks_are_rare(run: Run) -> None:
    """W-2 1.33's known gap (a prefetched break neither filled from its retrieval nor requested in real time) is
    tolerated above, each case printed, in at most :func:`harness_pods.gap_sessions_allowed` of the sessions: one
    of this run's three, since a fifth of three is none."""
    sessions = {viewer.sid for viewer in run.viewers.values()}
    gapped = gapped_sessions(set(run.unserved), sessions)
    assert len(gapped) <= gap_sessions_allowed(sessions), (
        f"{len(gapped)} of {len(sessions)} sessions had an unserved break: {sorted(run.unserved)}"
    )


def test_the_panel_answers_only_for_the_callers_own_session(run: Run) -> None:
    assert run.refused == {"other": 403, "guest": 403}
