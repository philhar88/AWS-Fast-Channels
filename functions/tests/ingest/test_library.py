from decimal import Decimal

from boto3.dynamodb.conditions import Attr

from fast_channels.functions.ingest import library


def test_keys_follow_the_contract() -> None:
    assert library.asset_key("bbb-1a2b3c", "standard@0123456789") == {
        "pk": "ASSET#bbb-1a2b3c",
        "sk": "LADDER#standard@0123456789",
    }
    assert library.asset_index("content", "standard@0123456789", library.AssetStatus.READY, "2026-10-16T12:00:00Z") == {
        "gsi1pk": "KIND#content#standard@0123456789",
        "gsi1sk": "READY#2026-10-16T12:00:00Z",
    }
    assert library.ladder_key("standard@0123456789") == {"pk": "LADDER#standard", "sk": "V#standard@0123456789"}
    assert library.ladder_index(library.LadderStatus.PROVISIONING, "standard@0123456789") == {
        "gsi1pk": "LADDERS",
        "gsi1sk": "PROVISIONING#standard@0123456789",
    }


def test_names_of_runtime_resources() -> None:
    assert library.job_template_name("fast", "standard@0123456789") == "fast-standard_0123456789"
    assert library.vod_source_name("filler-1a2b3c", "standard@0123456789") == "filler-1a2b3c-standard_0123456789"
    long = library.vod_source_name("a" * 48, "standard@0123456789")
    assert len(long) == 64 and long == library.vod_source_name("a" * 48, "standard@0123456789")


def test_runtime_tags() -> None:
    assert library.runtime_tags("fast", ladder_version="standard@0123456789", correlation_id="c1", asset="x") == {
        "fast:app": "fast",
        "fast:owner": "pipeline",
        "fast:ladder": "standard@0123456789",
        "fast:correlation-id": "c1",
        "fast:asset": "x",
    }


def test_dynamo_number_round_trip() -> None:
    stored = library.to_dynamo({"cost": 0.56, "n": 3, "nested": [{"x": 1.5}]})
    assert stored == {"cost": Decimal("0.56"), "n": 3, "nested": [{"x": Decimal("1.5")}]}
    assert library.from_dynamo(stored) == {"cost": 0.56, "n": 3, "nested": [{"x": 1.5}]}
    assert library.from_dynamo({"s": {"a"}}) == {"s": ["a"]}


def test_update_request_uses_its_own_placeholders() -> None:
    condition = Attr("correlation_id").eq("c1")
    request = library.update_request(
        {"pk": "p", "sk": "s"}, {"status": "READY", "cost": 0.5}, remove=("error",), ConditionExpression=condition
    )
    assert request == {
        "Key": {"pk": "p", "sk": "s"},
        "UpdateExpression": "SET #f0 = :f0, #f1 = :f1 REMOVE #r0",
        "ExpressionAttributeNames": {"#f0": "status", "#f1": "cost", "#r0": "error"},
        "ExpressionAttributeValues": {":f0": "READY", ":f1": Decimal("0.5")},
        "ConditionExpression": condition,
    }
    assert "REMOVE" not in library.update_request({"pk": "p"}, {"a": 1})["UpdateExpression"]
