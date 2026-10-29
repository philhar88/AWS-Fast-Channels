"""The own book's view of its lines' budgets (read in the background), and the refresh base it shares."""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from fast_channels.core.pds.book import Book, Line
from fast_channels.functions.pds.budget import ATTEMPTS, Budgets
from fast_channels.functions.pds.store import CLIENT_CONFIG, Refreshed

TABLE = "fastm-pds"
LINES = (
    Line(
        id="direct-1",
        advertiser="A",
        adomain="direct1.example",
        creatives=("direct1",),
        cpm=Decimal(8),
        budget=Decimal(100),
    ),
    Line(
        id="direct-2",
        advertiser="B",
        adomain="direct2.example",
        creatives=("direct2",),
        cpm=Decimal(6),
        budget=Decimal(1),
    ),
)
BOOK = Book(lines=LINES)


def inline(target: Callable[[], None]) -> None:
    target()


def client() -> Any:
    return boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")


def shard(line: str, n: int, remaining: str) -> dict[str, Any]:
    return {"pk": {"S": f"BUDGET#{line}"}, "sk": {"S": f"S#{n}"}, "remaining": {"N": remaining}}


def test_a_line_is_exhausted_when_no_shard_has_its_price() -> None:
    dynamodb = client()
    budgets = Budgets(TABLE, lines=lambda: LINES, client=dynamodb, spawn=inline)
    with Stubber(dynamodb) as stubber:
        stubber.add_response(
            "batch_get_item",
            {
                "Responses": {
                    TABLE: [
                        shard("direct-1", 0, "0.007"),
                        shard("direct-1", 3, "0.008"),
                        shard("direct-2", 1, "0.005"),
                        shard("direct-2", 2, "0"),
                        {"pk": {"S": "BUDGET#direct-2"}, "sk": {"S": "S#3"}},
                    ]
                }
            },
            {"RequestItems": {TABLE: {"Keys": ANY, "ProjectionExpression": "pk, sk, remaining"}}},
        )
        assert budgets.load() == {
            "direct-1": (Decimal("0.007"), Decimal("0.008")),
            "direct-2": (Decimal("0.005"), Decimal(0)),
        }

    assert budgets.exhausted(BOOK) == frozenset({"direct-2"})


def test_unread_lines_are_exhausted() -> None:
    budgets = Budgets(TABLE, lines=lambda: LINES, client=client(), spawn=lambda target: None)

    assert budgets.exhausted(BOOK) == frozenset({"direct-1", "direct-2"})


def test_unprocessed_keys_are_asked_again_a_few_times() -> None:
    dynamodb = client()
    budgets = Budgets(TABLE, lines=lambda: LINES[:1], client=dynamodb, spawn=inline)
    left = {TABLE: {"Keys": [{"pk": {"S": "BUDGET#direct-1"}, "sk": {"S": "S#1"}}]}}
    with Stubber(dynamodb) as stubber:
        stubber.add_response(
            "batch_get_item", {"Responses": {TABLE: [shard("direct-1", 0, "1")]}, "UnprocessedKeys": left}
        )
        for _ in range(ATTEMPTS - 1):
            stubber.add_response("batch_get_item", {"Responses": {TABLE: []}, "UnprocessedKeys": left})
        assert budgets.load() == {"direct-1": (Decimal(1),)}
        stubber.assert_no_pending_responses()


def test_a_failed_read_keeps_what_was_read(caplog: pytest.LogCaptureFixture) -> None:
    dynamodb = client()
    now = [0.0]
    budgets = Budgets(TABLE, lines=lambda: LINES[:1], client=dynamodb, clock=lambda: now[0], spawn=inline)
    with Stubber(dynamodb) as stubber:
        stubber.add_response("batch_get_item", {"Responses": {TABLE: [shard("direct-1", 0, "1")]}})
        stubber.add_client_error("batch_get_item", service_error_code="ProvisionedThroughputExceededException")
        budgets.load()
        now[0] += 10
        assert budgets.get() == {"direct-1": (Decimal(1),)}

    assert [getattr(r, "refreshed", None) for r in caplog.records if r.getMessage() == "refresh failed"] == ["budget"]


def test_the_default_client_has_short_timeouts() -> None:
    assert Budgets(TABLE, lines=lambda: ()).client.meta.config.read_timeout == CLIENT_CONFIG.read_timeout


def test_the_refresh_base_needs_a_fetch() -> None:
    class Plain(Refreshed[int]):
        pass

    with pytest.raises(NotImplementedError):
        Plain(0, every=1).load()
    assert Plain(0, every=1)._describe(0) == {}
    assert Plain(7, every=1).current == 7
