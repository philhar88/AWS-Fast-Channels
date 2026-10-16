"""``catalog`` target: the reference catalog (``ReferenceCatalog`` table) behind the bring-your-own-CMS contract.

Both tools are contract-identical to ``contracts/catalog.openapi.yaml``: the same names, descriptions, input and
output schemas (a contract test enforces it). Registering a CMS that implements that OpenAPI document as the
``catalog`` Gateway target therefore changes nothing for the agent.

Items (CONTRACTS §D): ``CONTENT#<content_id>`` / ``META`` holding the ContentItem attributes, with GSI1
``GENRE#<first genre>`` / ``<title>``.
"""

from functools import reduce
from operator import and_, or_
from typing import TYPE_CHECKING

from boto3.dynamodb.conditions import Attr, ConditionBase
from pydantic import BaseModel, Field

from fast_channels.core.catalog import CONTENT_ID_PATTERN, ContentItem, ContentPage, ContentQuery
from fast_channels.functions import aws
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import dispatch
from fast_channels.functions.tools.library_store import TABLE_KEYS, Item, collect, plain
from fast_channels.functions.tools.registry import Access, ToolContext, ToolError, tool

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table


class GetContentInput(BaseModel):
    """Path parameters of ``GET /content/{content_id}``."""

    content_id: str = Field(pattern=CONTENT_ID_PATTERN, description="Catalog content id.")


def catalog_item(content: ContentItem) -> Item:
    """The ``ReferenceCatalog`` item for ``content`` (what ``scripts/seed_sample_content.py`` writes)."""
    index = {"gsi1pk": f"GENRE#{content.genres[0]}", "gsi1sk": content.title} if content.genres else {}
    return {
        "pk": f"CONTENT#{content.content_id}",
        "sk": "META",
        **content.model_dump(mode="json", exclude_none=True),
        **index,
    }


@tool("catalog", access=Access.READ)
def search_content(query: ContentQuery, ctx: ToolContext) -> ContentPage:
    """Find titles to schedule. Filters combine with AND; genres and ratings match any listed value.
    Results are paged with next_token."""
    filters: list[ConditionBase] = [Attr("sk").eq("META")]
    if query.genres:
        filters.append(reduce(or_, (Attr("genres").contains(genre) for genre in query.genres)))
    if query.ratings:
        filters.append(Attr("rating").is_in(query.ratings))
    if query.max_duration_minutes is not None:
        filters.append(Attr("duration_ms").lte(round(query.max_duration_minutes * 60_000)))
    words = (query.text or "").lower().split()
    items, token = collect(
        _table().scan,
        {"FilterExpression": reduce(and_, filters)},
        limit=query.limit,
        next_token=query.next_token,
        key_names=TABLE_KEYS,
        keep=lambda item: _mentions(item, words),
    )
    return ContentPage(items=[ContentItem.model_validate(item) for item in items], next_token=token)


@tool("catalog", access=Access.READ)
def get_content(request: GetContentInput, ctx: ToolContext) -> ContentItem:
    """Full metadata of one title, including cue points, rights window and assets."""
    item = _table().get_item(Key={"pk": f"CONTENT#{request.content_id}", "sk": "META"}).get("Item")
    if not item:
        raise ToolError(
            "NOT_FOUND",
            f"no title {request.content_id} in the catalog",
            hint="Find titles with catalog___search_content.",
        )
    return ContentItem.model_validate(plain(item))


def _mentions(item: Item, words: list[str]) -> bool:
    """Whether every word occurs (case-insensitively) in the title or synopsis; DynamoDB filters are case-sensitive."""
    text = f"{item.get('title', '')} {item.get('synopsis', '')}".lower()
    return all(word in text for word in words)


def _table() -> Table:
    config = settings()
    config.require("catalog_table")
    return aws.table(config.catalog_table)


handler = dispatch.handler("catalog")
