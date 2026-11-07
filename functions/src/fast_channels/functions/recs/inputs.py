"""What a rail is made from: the guides, the catalog cards, the ``recommender`` registry entry and the kill switch.

Every read is cached for a short time in the execution environment: a guide changes when a schedule is published,
the registry entry and the flag when an operator edits them, and a few seconds of lag are fine for a rail.
"""

import json
import time
from collections.abc import Callable, Iterable, Sequence
from functools import partial
from typing import Any, Final

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities import parameters
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from fast_channels.core.flags import PROFILE, Flags, RecsStrategy
from fast_channels.core.model_registry import TaskEntry
from fast_channels.core.recs.cards import Card, card
from fast_channels.core.recs.rail import Airing, airings
from fast_channels.core.understanding.record import ITEM_SK
from fast_channels.functions.aws import client
from fast_channels.functions.epg import keys as epg_keys
from fast_channels.functions.recs.settings import recs_settings

logger = Logger()

GUIDE_SECONDS: Final = 30.0
"""The guides' CDN cache lasts as long (``functions.epg.CACHE_CONTROL``)."""
ENTRY_SECONDS: Final = 60.0
FLAG_SECONDS: Final = 30
BATCH_KEYS: Final = 100
"""``BatchGetItem`` reads at most 100 keys."""

_cache: dict[str, tuple[float, Any]] = {}
_DESERIALIZER: Final = TypeDeserializer()


def _cached[T](key: str, seconds: float, load: Callable[[], T]) -> T:
    now = time.monotonic()
    if (hit := _cache.get(key)) and now - hit[0] < seconds:
        return hit[1]  # type: ignore[no-any-return]
    value = load()
    _cache[key] = (now, value)
    return value


def guides(channels: Sequence[str] | None = None) -> list[list[Airing]]:
    """The channels' guides (``epg/<channel>.json``), in channel order; a channel without one has no airings."""
    config = recs_settings()

    def load(channel: str) -> list[Airing]:
        try:
            body = client("s3").get_object(Bucket=config.media_bucket, Key=epg_keys(channel)[0])["Body"].read()
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return []
            raise
        try:
            return airings(channel, json.loads(body))
        except json.JSONDecodeError, ValidationError:
            logger.warning("unreadable guide, left out", extra={"channel": channel})
            return []

    return [_cached(f"guide:{c}", GUIDE_SECONDS, partial(load, c)) for c in channels or config.channels]


def cards(content_ids: Iterable[str]) -> dict[str, Card]:
    """The cards of ``content_ids``: each title's catalog item (``META``) with its content understanding item
    (``UNDERSTANDING``) when there is one. Titles without a catalog item have no card."""
    config = recs_settings()
    keys = [
        {"pk": f"CONTENT#{content_id}", "sk": sk} for content_id in sorted(set(content_ids)) for sk in ("META", ITEM_SK)
    ]
    items: dict[tuple[str, str], dict[str, Any]] = {}
    dynamodb = client("dynamodb")
    for start in range(0, len(keys), BATCH_KEYS):
        request: dict[str, Any] = {
            config.catalog_table: {
                "Keys": [{k: {"S": v} for k, v in key.items()} for key in keys[start : start + BATCH_KEYS]]
            }
        }
        for _ in range(5):
            response = dynamodb.batch_get_item(RequestItems=request)
            for raw in response.get("Responses", {}).get(config.catalog_table, []):
                item = _plain(raw)
                items[(str(item["pk"]), str(item["sk"]))] = item
            request = response.get("UnprocessedKeys") or {}
            if not request:
                break
    found = {}
    for (pk, sk), item in items.items():
        if sk != "META":
            continue
        try:
            found[pk.removeprefix("CONTENT#")] = card(item, items.get((pk, ITEM_SK)))
        except KeyError, ValueError:
            logger.warning("unreadable catalog item, left out", extra={"pk": pk})
    return found


def _plain(item: dict[str, Any]) -> dict[str, Any]:
    """A low-level DynamoDB item as plain Python values."""
    deserializer = _DESERIALIZER
    return {key: deserializer.deserialize(value) for key, value in item.items()}


def task_entry() -> TaskEntry:
    """The ``recommender`` registry entry from SSM, or the entry as deployed when it cannot be read, is invalid or
    names another profile than the deployed one (only the deployed profile may be invoked)."""
    config = recs_settings()
    deployed = TaskEntry.model_validate_json(config.model_task_default)

    def load() -> TaskEntry:
        try:
            raw = client("ssm").get_parameter(Name=config.model_task_parameter)["Parameter"]["Value"]
            entry = TaskEntry.model_validate_json(raw)
        except ClientError, BotoCoreError, ValidationError:
            logger.warning("registry entry unusable, using the entry as deployed", exc_info=True)
            return deployed
        if entry.profile_arn != deployed.profile_arn or entry.model != deployed.model:
            logger.warning("registry entry names another model or profile, using the entry as deployed")
            return deployed
        return entry

    return _cached("entry", ENTRY_SECONDS, load)


def kill_switch() -> bool:
    """Whether ``recs.strategy`` is ``popularity`` (the ops module's flag); without ops there is no switch."""
    config = recs_settings()
    if not (config.appconfig_application and config.appconfig_environment):
        return False
    try:
        data = parameters.get_app_config(
            name=PROFILE,
            environment=config.appconfig_environment,
            application=config.appconfig_application,
            transform="json",
            max_age=FLAG_SECONDS,
        )
    except parameters.exceptions.GetParameterError:
        logger.warning("flags unreadable; the recommender runs", exc_info=True)
        return False
    return Flags.from_simplified(data if isinstance(data, dict) else {}).recs is RecsStrategy.POPULARITY
