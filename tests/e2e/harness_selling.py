"""The agentic-selling module's end-to-end harness (M7): the seller over A2A 1.0, the buyer agent on AgentCore Runtime,
the scripted buyer journey, and the booking's line and breaks (``test_21_selling``, ``test_24_creatives``)."""

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from _stack import Module, Stage
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from harness import BREAK_POLICY, LEAD, Channel, Player, Tools, wait_for
from harness_demand import DemandEdge
from harness_pods import (
    SMART_TV_USER_AGENT,
    Logs,
    Viewer,
    ipv4_client,
    json_object,
    prebid_tasks,
    record_pattern,
    warmups,
    watch,
)

from fast_channels.core.demand import warmup_project
from fast_channels.core.monetization import names as release_names
from fast_channels.core.monetization.release import PlaybackPlan
from fast_channels.core.pds import names as pds_names
from fast_channels.core.pds.records import DELIVERY_RECORD, SELECT_RECORD
from fast_channels.core.pds.snapshot import SNAPSHOT_KEY, Snapshot
from fast_channels.core.selling.booking import line_id

BRAND: Final = "acme-outdoor.example"
"""The brand the reference buyer books for (``modules.selling.buyer_brand``, the default); each run books for a
subdomain of its own, so that its line never shares an advertiser domain (and a pod) with an earlier run's."""
SELLER_ID: Final = "fast-seller"
"""The seller's id in the buyer's registry (``infra.constructs.selling.agents.SELLER_ID``)."""
TURN_TIMEOUT_S: Final = 600


@dataclass(frozen=True)
class Seller:
    """The seller's AdCP skills over A2A 1.0, as the reference buyer's principal."""

    url: str
    token: str

    def call(self, skill: str, **arguments: Any) -> dict[str, Any]:
        """One skill's AdCP response (the last data part of the task's first artifact)."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "message": {
                    "role": "ROLE_USER",
                    "parts": [{"data": {"skill": skill, "input": arguments}}],
                    "messageId": f"msg-{uuid.uuid4()}",
                }
            },
        }
        headers = {"Authorization": f"Bearer {self.token}", "A2A-Version": "1.0"}
        response = httpx.post(f"{self.url}/a2a", json=body, headers=headers, timeout=60.0)
        response.raise_for_status()
        answer = response.json()
        if "error" in answer:
            raise AssertionError(f"{skill}: {answer['error']}")
        task = answer["result"].get("task") or answer["result"]
        parts = [part["data"] for part in task["artifacts"][0]["parts"] if "data" in part]
        found: dict[str, Any] = parts[-1]
        return found


@dataclass(frozen=True)
class Buyer:
    """The AWS reference buyer on AgentCore Runtime (A2A, SigV4)."""

    session: Any
    runtime_arn: str
    sessions_table: str

    def turn(self, prompt: str, session_id: str) -> dict[str, Any]:
        """One A2A ``message/send`` turn (the runtime's A2A server speaks 0.3); the JSON-RPC answer."""
        client = self.session.client(
            "bedrock-agentcore", config=Config(read_timeout=TURN_TIMEOUT_S, retries={"max_attempts": 1})
        )
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": prompt}],
                },
                "metadata": {"invokerAgent": "FAST Channels end-to-end tests"},
            },
        }
        response = client.invoke_agent_runtime(
            agentRuntimeArn=self.runtime_arn,
            runtimeSessionId=session_id,
            qualifier="DEFAULT",
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(body).encode(),
        )
        answer: dict[str, Any] = json.loads(response["response"].read())
        return answer

    def calls(self, session_id: str) -> list[dict[str, Any]]:
        """The tool calls the buyer recorded for a session, in order (its sessions table)."""
        table = self.session.resource("dynamodb").Table(self.sessions_table)
        items = table.query(KeyConditionExpression=Key("pk").eq(f"SESSION#{session_id}"))["Items"]
        return sorted(items, key=lambda item: str(item["sk"]))


def reply(answer: dict[str, Any]) -> str:
    """The text of an A2A answer's artifacts (or of its error)."""
    if "error" in answer:
        return json.dumps(answer["error"])
    result = answer.get("result") or {}
    return "".join(
        str(part.get("text") or json.dumps(part.get("data")))
        for artifact in result.get("artifacts") or []
        for part in artifact.get("parts") or []
    )


def journey(*, brand: str, product_id: str, plan_id: str, budget: float, starts: datetime, ends: datetime) -> str:
    """The scripted buyer journey: discover, get the products, bind the account, plan, check, book."""
    flight = f"flight_start {starts.isoformat()} and flight_end {ends.isoformat()}"
    return f"""You are running a scripted test booking. Do not ask questions; do each step in order with exactly
these values, using seller_id "{SELLER_ID}" wherever a tool takes one.
1. Call adcp_get_capabilities.
2. Call adcp_sync_accounts with brand_domain "{brand}" and operator "{brand}".
3. Call adcp_get_products with the brief "30 s CTV mid-roll on a FAST channel" and brand_domain "{brand}". Use the
   product "{product_id}" and the pricing_option_id of its first pricing option.
4. Call adcp_sync_plans with plan_id "{plan_id}", brand_domain "{brand}", objectives "Awareness on FAST CTV",
   budget_total {budget * 2}, {flight}, and channels ["ctv"].
5. Call adcp_check_governance with plan_id "{plan_id}", tool_name "create_media_buy" and the payload
   {{"packages": [{{"product_id": "{product_id}", "budget": {budget}, "pricing_option_id": <from step 3>}}],
   "start_time": "{starts.isoformat()}", "end_time": "{ends.isoformat()}"}}.
6. If the verdict is approved, call adcp_create_media_buy with plan_id "{plan_id}", the governance_context of step 5,
   product_id "{product_id}", budget {budget}, the same pricing_option_id, brand_domain "{brand}", operator
   "{brand}", and {flight}.
7. Reply with one line: MEDIA_BUY_ID=<the media_buy_id>."""


def delivery_question(media_buy_id: str) -> str:
    """The buyer's delivery check, after the breaks aired."""
    return (
        f'Call adcp_get_media_buy_delivery with seller_id "{SELLER_ID}" and media_buy_id "{media_buy_id}". Reply with '
        "one line: IMPRESSIONS=<total impressions> SPEND=<total spend>."
    )


# --- a booking, its line and its breaks -------------------------------------------------------------------------------

BUDGET: Final = 50.0
"""The booked budget, US dollars (simulated): 1,250 impressions at the fastcisell stage's $40 CPM."""
FLIGHT_LEAD: Final = timedelta(minutes=4)
"""The flight starts this long after the journey: create_media_buy refuses a start in the past."""
TURNS: Final = 3
"""Turns the buyer gets to book (it may stop to report a step)."""
LINE_TIMEOUT_S: Final = 300
"""The bridge syncs every minute, then the publisher runs (seconds) and the sidecars read the snapshot (30 s)."""
READY_TIMEOUT_S: Final = 1500
LOGS_TIMEOUT_S: Final = 420
VIEWERS: Final = 2
BREAKS: Final = 2
TAIL: Final = timedelta(seconds=90)
MEDIA_BUY: Final = re.compile(r"MEDIA_BUY_ID=(buy_[0-9a-f]+)")


def seller_of(stage: Stage) -> Seller:
    """The stage's seller, as the reference buyer's principal (its bearer token)."""
    secrets = stage.session.client("secretsmanager", region_name=stage.region)
    token = secrets.get_secret_value(SecretId=stage.output(Module.SELLING, "BuyerTokenSecret"))["SecretString"]
    return Seller(stage.output(Module.SELLING, "SellerUrl"), token)


def buyer_of(stage: Stage) -> Buyer:
    """The stage's reference buyer agent."""
    return Buyer(
        stage.session,
        stage.output(Module.SELLING, "BuyerRuntimeArn"),
        stage.output(Module.SELLING, "BuyerSessionsTable"),
    )


def run_brand() -> str:
    """A brand domain of this run's own (a subdomain of :data:`BRAND`)."""
    return f"e2e-{uuid.uuid4().hex[:8]}.{BRAND}"


def account(brand: str) -> dict[str, Any]:
    """The account the buyer's journey syncs: its brand, which is also its operator."""
    return {"brand": {"domain": brand}, "operator": brand}


@dataclass
class Booking:
    """What the buyer booked, as the seller and the own book know it."""

    media_buy_id: str
    session_id: str
    calls: list[dict[str, Any]]
    line: str
    starts: datetime
    brand: str


def seller_buy(seller: Seller, media_buy_id: str, brand: str) -> dict[str, Any] | None:
    """The seller's media buy (its account is the brand's), or None."""
    found = seller.call("get_media_buys", account=account(brand), media_buy_ids=[media_buy_id]).get("media_buys") or []
    return next((buy for buy in found if buy["media_buy_id"] == media_buy_id), None)


def book(seller: Seller, buyer: Buyer, *, brand: str, product: str) -> Booking:
    """The scripted journey, run by the buyer agent for ``brand``; the buy it made, read back from the seller."""
    starts = (datetime.now(UTC) + FLIGHT_LEAD).replace(microsecond=0)
    prompt = journey(
        brand=brand,
        product_id=product,
        plan_id=f"plan-e2e-{uuid.uuid4().hex[:8]}",
        budget=BUDGET,
        starts=starts,
        ends=starts + timedelta(days=1),
    )
    session_id = f"fast-e2e-selling-{uuid.uuid4().hex}"
    text, media_buy_id = "", None
    for turn in range(TURNS):
        answer = buyer.turn(prompt if turn == 0 else "Continue the steps and reply with MEDIA_BUY_ID.", session_id)
        text = reply(answer)
        print(f"buyer turn {turn + 1}: {text[:600]}")
        if (said := MEDIA_BUY.search(text)) and seller_buy(seller, said[1], brand) is not None:
            media_buy_id = said[1]
            break
    assert media_buy_id, f"the seller has no buy the buyer named after {TURNS} turns: {text[:1000]}"
    return Booking(media_buy_id, session_id, buyer.calls(session_id), line_id(media_buy_id, 1), starts, brand)


def snapshot(stage: Stage) -> Snapshot:
    """The PDS's published snapshot."""
    s3 = stage.session.client("s3", region_name=stage.region)
    ssm = stage.session.client("ssm", region_name=stage.region)
    name = pds_names.parameter_name(stage.prefix, pds_names.Parameter.CONFIG_BUCKET)
    bucket = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    return Snapshot.model_validate_json(s3.get_object(Bucket=bucket, Key=SNAPSHOT_KEY)["Body"].read())


def snapshot_line(stage: Stage, line: str, *, crid: str | None = None) -> dict[str, Any]:
    """The booked ``line`` once the snapshot has it (serving ``crid``, when given)."""

    def published() -> dict[str, Any] | None:
        found = next((candidate for candidate in snapshot(stage).lines if candidate.id == line), None)
        if found is None or (crid is not None and crid not in found.creatives):
            return None
        return found.model_dump(mode="json")

    return wait_for(published, timeout=LINE_TIMEOUT_S, interval=10, what=f"line {line} in the snapshot")


@dataclass
class Aired:
    """What the viewers' breaks did with the booked line."""

    crid: str
    creative_id: str
    stitched: list[dict[str, Any]]
    """The viewers' ``FILLED_AVAIL`` events."""
    deliveries: list[dict[str, Any]]
    """The ``pds.delivery`` records of the viewers' sessions."""
    selects: list[dict[str, Any]]
    """The ``pds.select`` records of the viewers' sessions."""


def air(
    stage: Stage,
    booking: Booking,
    channel: Channel,
    tools: Tools,
    ingest: Callable[[], Any],
    *,
    crid: str,
    creative_id: str,
) -> Aired:
    """Air the pattern with two 60.06 s breaks to two smart-TV viewers on the stable sibling, once the booking's flight
    started; stop the channel; read what the viewers' breaks stitched, decided and delivered."""
    edge = DemandEdge(stage.prefix, stage.session)
    with ipv4_client(follow_redirects=True, timeout=20.0, headers={"User-Agent": SMART_TV_USER_AGENT}) as http:
        try:
            tools("channels___start_channel", channel=channel.name)
            asset = ingest()  # while Prebid Server scales out
            wait_until_ready(stage, edge)
            wait_for(
                lambda: True if datetime.now(UTC) >= booking.starts else None,
                timeout=FLIGHT_LEAD.total_seconds() + 60,
                interval=10,
                what="the flight's start",
            )
            ends = schedule(tools, channel.name, asset.asset_id)
            plan = playback_plan(stage, channel.name)
            joined = datetime.now(UTC)
            viewers = [Viewer.join(Player(client=http), plan.playback["hls"]) for _ in range(VIEWERS)]
            watch(viewers, ends=ends + TAIL)
        finally:
            tools("channels___stop_channel", channel=channel.name)
    sids = {viewer.sid for viewer in viewers}
    logs = Logs(
        stage.session.client("logs", region_name=stage.region), start_ms=int(joined.timestamp() * 1000) - 60_000
    )
    ads_group = stage.output(Module.RELEASE, "SiblingLogGroup")
    delivery_group = function_log_group(stage, stage.output(Module.PDS, "DeliveryFunction"))
    deadline = time.monotonic() + LOGS_TIMEOUT_S
    while True:
        stitched = [
            found
            for event in logs.events(ads_group, '{ $.eventType = "FILLED_AVAIL" }')
            if (found := json_object(str(event["message"]))) and found.get("sessionId") in sids
        ]
        deliveries = [
            found
            for event in logs.events(delivery_group, record_pattern(DELIVERY_RECORD, sids))
            if (found := json_object(str(event["message"])))
        ]
        if any(d.get("line") == booking.line for d in deliveries) or time.monotonic() > deadline:
            break
        time.sleep(20)
    selects = [
        found
        for event in logs.events(pds_names.sidecar_log_group(stage.prefix), record_pattern(SELECT_RECORD, sids))
        if (found := json_object(str(event["message"])))
    ]
    return Aired(crid, creative_id, stitched, deliveries, selects)


def wait_until_ready(stage: Stage, edge: DemandEdge) -> None:
    """Until Prebid Server runs and every running task's warm-up succeeded (the PDS is their sidecar)."""
    ecs, codebuild = stage.session.client("ecs"), stage.session.client("codebuild")

    def ready() -> bool | None:
        tasks = prebid_tasks(ecs, cluster=edge.cluster, service=edge.service)
        warmed = warmups(codebuild, warmup_project(stage.prefix), tasks)
        return True if tasks and len(warmed) == len(tasks) and set(warmed.values()) == {"SUCCEEDED"} else None

    wait_for(ready, timeout=READY_TIMEOUT_S, interval=20, what="a warm Prebid Server")


def schedule(tools: Tools, channel: str, asset_id: str) -> datetime:
    """The pattern twice, each with a 60.06 s break, starting in a few minutes; when the last program ends."""
    start = (datetime.now(UTC) + LEAD).replace(microsecond=0)
    plan = tools(
        "schedule___plan",
        channel=channel,
        picks=[asset_id] * BREAKS,
        start=start.isoformat(),
        break_policy=BREAK_POLICY,
    )
    assert plan["publishable"], f"plan {plan['plan_id']} has errors: {plan['violations']}"
    programs = tools("schedule___publish", plan_id=plan["plan_id"])["programs"]
    return max(datetime.fromisoformat(str(p["end"])) for p in programs)


def playback_plan(stage: Stage, channel: str) -> PlaybackPlan:
    """The channel's playback plan, read through the CDN as a player reads it."""
    ssm = stage.session.client("ssm", region_name=stage.region)
    name = release_names.parameter_name(stage.prefix, release_names.Parameter.CDN_BASE_URL)
    cdn = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    with ipv4_client(timeout=10.0) as http:
        response = http.get(f"{cdn.rstrip('/')}/{release_names.plan_key(channel)}")
    response.raise_for_status()
    return PlaybackPlan.model_validate_json(response.text)


def function_log_group(stage: Stage, function: str) -> str:
    configuration = stage.session.client("lambda", region_name=stage.region).get_function_configuration(
        FunctionName=function
    )
    return str(configuration["LoggingConfig"]["LogGroup"])
