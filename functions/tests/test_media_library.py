from decimal import Decimal

from boto3.dynamodb.conditions import Attr

from fast_channels.functions import media_library as schema
from fast_channels.functions.tools import library_store


def test_keys_follow_the_contract() -> None:
    assert schema.asset_key("bbb-1a2b3c", "standard@0123456789") == {
        "pk": "ASSET#bbb-1a2b3c",
        "sk": "LADDER#standard@0123456789",
    }
    assert schema.asset_index("content", "standard@0123456789", schema.AssetStatus.READY, "2026-10-16T12:00:00Z") == {
        "gsi1pk": "KIND#content#standard@0123456789",
        "gsi1sk": "READY#2026-10-16T12:00:00Z",
    }
    assert schema.ladder_key("standard@0123456789") == {"pk": "LADDER#standard", "sk": "V#standard@0123456789"}
    assert schema.ladder_index(schema.LadderStatus.PROVISIONING, "standard@0123456789") == {
        "gsi1pk": "LADDERS",
        "gsi1sk": "PROVISIONING#standard@0123456789",
    }
    assert schema.channel_key("fast-demo") == {"pk": "CHANNEL#fast-demo", "sk": "META"}
    assert schema.channel_index(schema.Owner.IAC, "fast-demo") == {"gsi1pk": "CHANNELS", "gsi1sk": "iac#fast-demo"}
    assert schema.program_key("fast-demo", 1_792_000_000_000) == {
        "pk": "CHANNEL#fast-demo",
        "sk": "PROGRAM#1792000000000",
    }
    assert schema.program_sk(5) == "PROGRAM#0000000000005"  # zero-padded: sort order is start order
    assert schema.program_index("p1", "fast-demo") == {"gsi1pk": "PROGRAM#p1", "gsi1sk": "fast-demo"}
    assert schema.plan_key("fast-demo", "fast-demo.ab") == {"pk": "CHANNEL#fast-demo", "sk": "PLAN#fast-demo.ab"}
    assert schema.lease_key("fast-demo") == {"pk": "CHANNEL#fast-demo", "sk": "LEASE"}
    assert schema.proposal_key("proposal-1") == {"pk": "PROPOSAL#proposal-1", "sk": "META"}


def test_program_items_expire_a_month_after_the_program_ends() -> None:
    assert schema.program_expires_at(1_792_000_000_999) == 1_792_000_000 + 30 * 86_400


def test_the_tools_store_uses_the_same_schema() -> None:
    assert library_store.ladder_key is schema.ladder_key
    assert library_store.AssetStatus is schema.AssetStatus
    assert library_store.plain is schema.from_dynamo and library_store.decimals is schema.to_dynamo


def test_dynamo_number_round_trip() -> None:
    stored = schema.to_dynamo({"cost": 0.56, "n": 3, "nested": [{"x": 1.5}], "t": (0.5,)})
    assert stored == {"cost": Decimal("0.56"), "n": 3, "nested": [{"x": Decimal("1.5")}], "t": [Decimal("0.5")]}
    assert schema.from_dynamo(stored) == {"cost": 0.56, "n": 3, "nested": [{"x": 1.5}], "t": [0.5]}
    assert schema.from_dynamo({"s": {"a"}}) == {"s": ["a"]}


def test_update_request_uses_its_own_placeholders() -> None:
    condition = Attr("correlation_id").eq("c1")
    request = schema.update_request(
        {"pk": "p", "sk": "s"}, {"status": "READY", "cost": 0.5}, remove=("error",), ConditionExpression=condition
    )
    assert request == {
        "Key": {"pk": "p", "sk": "s"},
        "UpdateExpression": "SET #f0 = :f0, #f1 = :f1 REMOVE #r0",
        "ExpressionAttributeNames": {"#f0": "status", "#f1": "cost", "#r0": "error"},
        "ExpressionAttributeValues": {":f0": "READY", ":f1": Decimal("0.5")},
        "ConditionExpression": condition,
    }
    assert "REMOVE" not in schema.update_request({"pk": "p"}, {"a": 1})["UpdateExpression"]
    assert schema.update_request({"pk": "p"}, {}, remove=["brief"]) == {
        "Key": {"pk": "p"},
        "UpdateExpression": "REMOVE #r0",
        "ExpressionAttributeNames": {"#r0": "brief"},
    }
