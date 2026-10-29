"""Link new ledger entries into their hash chains and archive them (DESIGN-v2.2 §18.5).

The ``<prefix>-ledger`` table's stream invokes this function with the changes of entry items only (the event source
mapping's filter: ``type = entry``), in order per item. For each new entry:

1. If its ``L#<record id>`` item exists, an earlier, partly failed invocation linked it: archive that record again.
2. Otherwise link it after the chain's ``HEAD`` (``core.ledger.link``) and write the new head and the ``L#`` item in
   one transaction, conditional on the head still being the one read and the ``L#`` item being new. A concurrent
   writer for the same chain (another stream shard) cancels the transaction: the record is reported as a batch item
   failure and retried, and then links after the new head.
3. Put the linked record (one JSON line) on the Firehose stream that archives the ledger under S3 Object Lock.

A record can reach the archive twice (step 1), byte for byte the same, which ``core.ledger.verify`` accepts; it never
reaches it at two positions. The first failure ends the batch: that record and every later one are retried, so a
chain never links out of stream order within a shard.

An entry that changes or disappears (``MODIFY``, ``REMOVE``: something wrote around the conditional put) is logged as
an error and counted in ``LedgerEntriesChanged``; its first version is already chained and archived. An item that is
not a valid entry is logged and counted in ``LedgerEntriesRejected`` and skipped, so it cannot block its chain.
"""

from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.data_classes import DynamoDBStreamEvent, event_source
from aws_lambda_powertools.utilities.data_classes.dynamo_db_stream_event import DynamoDBRecordEventName
from botocore.exceptions import ClientError
from pydantic import ValidationError

from fast_channels.core.ledger import (
    HEAD_KEY,
    LINK_KEY_PREFIX,
    ChainedRecord,
    ChainHead,
    LedgerEntry,
    entry_from_item,
    link,
)
from fast_channels.functions import aws
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.data_classes.dynamo_db_stream_event import DynamoDBRecord
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
metrics = Metrics()

_CONFLICTS: Final = frozenset({"TransactionCanceledException", "ConditionalCheckFailedException"})


class ChainConflictError(RuntimeError):
    """Another writer moved the chain's head first."""


@metrics.log_metrics
@logger.inject_lambda_context
@event_source(data_class=DynamoDBStreamEvent)
def handler(event: DynamoDBStreamEvent, context: LambdaContext) -> dict[str, list[dict[str, str]]]:
    """Archive the batch's new entries in stream order; report the first failure, so it and what follows retry."""
    settings().require("ledger_table", "ledger_stream")
    archived = changed = rejected = 0
    failure: list[dict[str, str]] = []
    for record in event.records:
        sequence = record.dynamodb.sequence_number if record.dynamodb else None
        try:
            match record.event_name:
                case DynamoDBRecordEventName.INSERT:
                    if archive(record) is None:
                        rejected += 1
                    else:
                        archived += 1
                case _:
                    changed += 1
                    _changed(record)
        except (ChainConflictError, ClientError) as error:
            logger.warning("ledger record not archived; retrying from it", extra={"error": str(error)})
            failure = [{"itemIdentifier": str(sequence)}]
            break
    metrics.add_metric(name="LedgerRecordsArchived", unit=MetricUnit.Count, value=archived)
    metrics.add_metric(name="LedgerEntriesChanged", unit=MetricUnit.Count, value=changed)
    metrics.add_metric(name="LedgerEntriesRejected", unit=MetricUnit.Count, value=rejected)
    return {"batchItemFailures": failure}


def archive(record: DynamoDBRecord) -> ChainedRecord | None:
    """Link the record's new entry (unless it already is) and archive it; ``None`` for an item that is no entry.

    Raises:
        ChainConflictError: another writer moved the chain's head.
        botocore.exceptions.ClientError: DynamoDB or Firehose failed.
    """
    image: dict[str, Any] = (record.dynamodb.new_image if record.dynamodb else None) or {}
    try:
        entry = entry_from_item(image)
    except (ValueError, ValidationError) as error:
        logger.error(
            "not a valid ledger entry; skipped",
            extra={"pk": image.get("pk"), "sk": image.get("sk"), "error": str(error)},
        )
        return None
    logger.set_correlation_id(entry.correlation_id)
    chained = linked(entry) or commit(entry)
    aws.client("firehose").put_record(
        DeliveryStreamName=settings().ledger_stream, Record={"Data": chained.line().encode()}
    )
    logger.info(
        "ledger record archived",
        extra={"chain_id": chained.chain_id, "seq": chained.seq, "record_id": chained.record_id, "hash": chained.hash},
    )
    return chained


def linked(entry: LedgerEntry) -> ChainedRecord | None:
    """The record an earlier invocation linked ``entry`` as, if any."""
    item = aws.table(settings().ledger_table).get_item(
        Key={"pk": entry.chain_id, "sk": f"{LINK_KEY_PREFIX}{entry.record_id}"}, ConsistentRead=True
    )
    found = item.get("Item")
    return ChainedRecord.model_validate_json(str(found["record"])) if found else None


def head(chain_id: str) -> ChainHead | None:
    """The chain's head (strongly consistent), or ``None`` for a new chain."""
    item = aws.table(settings().ledger_table).get_item(Key={"pk": chain_id, "sk": HEAD_KEY}, ConsistentRead=True)
    found = item.get("Item")
    if not found:
        return None
    return ChainHead(
        chain_id=chain_id, seq=int(str(found["seq"])), hash=str(found["hash"]), record_id=str(found["record_id"])
    )


def commit(entry: LedgerEntry) -> ChainedRecord:
    """Link ``entry`` after its chain's head and store the new head with the linked record, atomically.

    Raises:
        ChainConflictError: the head moved since it was read, or the record was linked meanwhile.
    """
    previous = head(entry.chain_id)
    chained = link(entry, previous)
    name = settings().ledger_table
    head_put: dict[str, Any] = {
        "TableName": name,
        "Item": {
            "pk": entry.chain_id,
            "sk": HEAD_KEY,
            "type": "head",
            "seq": chained.seq,
            "hash": chained.hash,
            "record_id": chained.record_id,
        },
    }
    if previous is None:
        head_put["ConditionExpression"] = "attribute_not_exists(pk)"
    else:
        head_put["ConditionExpression"] = "seq = :seq AND #hash = :hash"
        head_put["ExpressionAttributeNames"] = {"#hash": "hash"}
        head_put["ExpressionAttributeValues"] = {":seq": previous.seq, ":hash": previous.hash}
    link_put = {
        "TableName": name,
        "Item": {
            "pk": entry.chain_id,
            "sk": f"{LINK_KEY_PREFIX}{entry.record_id}",
            "type": "link",
            "seq": chained.seq,
            "record": chained.model_dump_json(),
        },
        "ConditionExpression": "attribute_not_exists(pk)",
    }
    try:
        # The table resource's client (de)serializes Python values, which its low-level type stubs do not show.
        client: Any = aws.table(name).meta.client
        client.transact_write_items(TransactItems=[{"Put": head_put}, {"Put": link_put}])
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in _CONFLICTS:
            raise ChainConflictError(
                f"chain {entry.chain_id} moved past seq {previous.seq if previous else 0}"
            ) from None
        raise
    return chained


def _changed(record: DynamoDBRecord) -> None:
    images = record.dynamodb
    old = (images.old_image if images else None) or {}
    logger.error(
        "a ledger entry was changed or removed after it was appended; its first version is chained",
        extra={"event": str(record.event_name), "pk": old.get("pk"), "sk": old.get("sk")},
    )
