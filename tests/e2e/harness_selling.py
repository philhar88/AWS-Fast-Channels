"""The agentic-selling module's end-to-end harness (M7): the seller over A2A 1.0, the buyer agent on AgentCore Runtime,
and the scripted buyer journey (``test_21_selling``)."""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import httpx
from boto3.dynamodb.conditions import Key
from botocore.config import Config

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
