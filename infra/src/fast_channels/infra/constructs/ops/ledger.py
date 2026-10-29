"""The hash-chained ledger (DESIGN-v2.2 §18.5, ADR-0022): table → stream → writer → Firehose → S3 Object Lock.

- :class:`LedgerStorage` (Data stack): the ``<prefix>-ledger`` table that producers append entries to (conditional
  puts; :meth:`LedgerStorage.grant_append`), with a stream of new and old images, and the archive bucket.
- :class:`LedgerWriter` (Ops stack): ``fast_channels.functions.ops.ledger_writer`` on the stream (entries only),
  which links each entry into its chain and puts the linked record on the ``<prefix>-ledger`` Firehose stream, which
  writes gzip JSON lines to ``ledger/<yyyy>/<MM>/<dd>/`` in the archive.

With ``ledger_lock`` ``governance`` or ``compliance`` the archive has S3 Object Lock with that default mode and
``ledger_retention_days``, and is **retained** when its stack is deleted: CloudFormation could not delete locked
versions, and a ledger must outlive the stack that wrote it. ``scripts/ledger_verify.py purge`` deletes a destroyed
stage's archive once its retention has passed (governance mode: earlier, with ``--bypass-governance``). With ``off``
the archive is a versioned bucket that follows ``removal``.
"""

from typing import Final

from aws_cdk import Duration, RemovalPolicy, Size, Tags
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kinesisfirehose as firehose
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from fast_channels.infra import nag
from fast_channels.infra.config import LedgerLock
from fast_channels.infra.constructs.alerts import PERIOD, alarm, dead_letter_alarm
from fast_channels.infra.constructs.functions import METRICS_NAMESPACE, FastFunction
from fast_channels.infra.constructs.ops.lake import LEDGER_PREFIX

__all__ = ["ARCHIVE_PREFIX", "ARCHIVE_TAG", "LedgerStorage", "LedgerWriter"]

ARCHIVE_PREFIX: Final = LEDGER_PREFIX
"""Key prefix of the archived records: ``ledger/<yyyy>/<MM>/<dd>/<stream>-…gz``."""
ARCHIVE_TAG: Final = "fast:ledger-archive"
"""Tag on the archive bucket (value ``true``), which outlives its stack."""
_BATCH_SIZE: Final = 100
_WRITER_SERVICE: Final = "ops.ledger_writer"
"""``service`` dimension of the writer's metrics (Powertools: the handler's module path)."""
MAX_LAG: Final = Duration.minutes(10)
"""Stream iterator age above which the archive is considered behind."""


class LedgerStorage(Construct):
    """The ledger table and its archive bucket.

    Table ``<prefix>-ledger``: ``pk`` (chain id) and ``sk`` (``E#<record id>`` entries, ``L#<record id>`` linked
    records, ``HEAD``), on demand, PITR, a stream of new and old images; deletion protection with ``removal: retain``.

    Args:
        prefix: deployment prefix.
        logs_bucket: receives the archive's access logs (``s3/ledger/``).
        lock: Object Lock mode of the archive.
        retention_days: Object Lock default retention.
        removal_policy: what happens to the table (and an unlocked archive) when the stack is deleted.

    Attributes:
        table: the ledger table. archive: the archive bucket.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        logs_bucket: s3.IBucket,
        lock: LedgerLock,
        retention_days: int,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.table = dynamodb.TableV2(
            self,
            "Table",
            table_name=f"{prefix}-ledger",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            dynamo_stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
            removal_policy=removal_policy,
            deletion_protection=removal_policy == RemovalPolicy.RETAIN,
        )
        locked = lock is not LedgerLock.OFF
        retention = Duration.days(retention_days)
        self.archive = s3.Bucket(
            self,
            "Archive",
            encryption=s3.BucketEncryption.S3_MANAGED,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            versioned=True,
            object_lock_enabled=locked or None,
            object_lock_default_retention=(
                s3.ObjectLockRetention.compliance(retention)
                if lock is LedgerLock.COMPLIANCE
                else s3.ObjectLockRetention.governance(retention)
            )
            if locked
            else None,
            removal_policy=RemovalPolicy.RETAIN if locked else removal_policy,
            auto_delete_objects=not locked and removal_policy == RemovalPolicy.DESTROY,
            server_access_logs_bucket=logs_bucket,
            server_access_logs_prefix="s3/ledger/",
            target_object_key_format=s3.TargetObjectKeyFormat.partitioned_prefix(s3.PartitionDateSource.EVENT_TIME),
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="abort-incomplete-uploads", abort_incomplete_multipart_upload_after=Duration.days(7)
                )
            ],
        )
        Tags.of(self.archive).add(ARCHIVE_TAG, "true")

    def grant_append(self, grantee: iam.IGrantable) -> None:
        """Let ``grantee`` append entries (``PutItem`` only: it can neither update nor delete one)."""
        grantee.grant_principal.add_to_principal_policy(
            iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[self.table.table_arn])
        )


class LedgerWriter(Construct):
    """The writer on the ledger table's stream, and the Firehose stream into the archive.

    The event source mapping passes only entry items (``type = entry``) and retries from the first failed record
    (batch item failures) until it succeeds or the stream drops it after 24 hours; records it gives up on go to a
    dead-letter queue. Firehose buffers up to a minute or 1 MiB and writes gzip JSON lines.

    Alarms (to ``topic``, missing data not breaching): ``<prefix>-ledger-dead-letters`` (a record the writer gave
    up on), ``<prefix>-ledger-entries-changed`` (an entry was changed or removed after it was appended),
    ``<prefix>-ledger-entries-rejected`` (an item that is no valid entry) and ``<prefix>-ledger-lag`` (the stream's
    iterator age above :data:`MAX_LAG`).

    Args:
        prefix: deployment prefix.
        table: the ledger table (with its stream).
        archive: the archive bucket.
        topic: the stage's alert topic.
        log_retention: retention of the writer's and the stream's logs.
        removal_policy: what happens to those logs when the stack is deleted.

    Attributes:
        function: the writer. stream: the Firehose stream. dead_letters: records the writer gave up on.
        alarms: the alarms by name.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        table: dynamodb.ITableV2,
        archive: s3.IBucket,
        topic: sns.ITopic,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        stream_logs = logs.LogGroup(
            self,
            "StreamLogs",
            log_group_name=f"/aws/vendedlogs/firehose/{prefix}-ledger",
            retention=log_retention,
            removal_policy=removal_policy,
        )
        # The S3 destination grants its role read-write on the bucket; nothing that writes the ledger may delete from
        # its archive, so the role denies it explicitly (an explicit Deny wins).
        stream_role = iam.Role(self, "StreamRole", assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"))
        stream_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["s3:DeleteObject*", "s3:PutBucket*", "s3:PutObjectRetention", "s3:PutObjectLegalHold"],
                resources=[archive.bucket_arn, archive.arn_for_objects("*")],
            )
        )
        self.stream = firehose.DeliveryStream(
            self,
            "Stream",
            delivery_stream_name=f"{prefix}-ledger",
            encryption=firehose.StreamEncryption.aws_owned_key(),
            destination=firehose.S3Bucket(
                archive,
                role=stream_role,
                data_output_prefix=f"{ARCHIVE_PREFIX}!{{timestamp:yyyy/MM/dd}}/",
                error_output_prefix="ledger-errors/!{firehose:error-output-type}/!{timestamp:yyyy/MM/dd}/",
                buffering_interval=Duration.seconds(60),
                buffering_size=Size.mebibytes(1),
                compression=firehose.Compression.GZIP,
                logging_config=firehose.EnableLogging(stream_logs),
            ),
        )
        nag.acknowledge_references(
            stream_role,
            {
                **{
                    nag.wildcard_action(action): "Firehose's S3 destination grants read-write on its bucket; deletes "
                    "and retention changes are denied explicitly"
                    for action in ("s3:Abort*", "s3:DeleteObject*", "s3:GetBucket*", "s3:GetObject*", "s3:List*")
                },
                nag.wildcard_resource(archive.arn_for_objects("*")): "Firehose writes objects under generated keys",
            },
        )
        self.function = FastFunction(
            self,
            "Writer",
            handler="fast_channels.functions.ops.ledger_writer.handler",
            description="Link new ledger entries into their hash chains and archive them",
            environment={"LEDGER_TABLE": table.table_name, "LEDGER_STREAM": self.stream.delivery_stream_name},
            memory_size=256,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        self.function.allow(["dynamodb:GetItem", "dynamodb:PutItem"], [table.table_arn])
        self.stream.grant_put_records(self.function.function)
        self.dead_letters = sqs.Queue(
            self,
            "DeadLetters",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        nag.acknowledge(
            self.dead_letters,
            {
                nag.SQS_DLQ: "This is the dead-letter queue of the stream's records",
                "Serverless-SQSRedrivePolicy": "This is the dead-letter queue of the stream's records",
            },
        )
        self.function.function.add_event_source(
            event_sources.DynamoEventSource(
                table,
                starting_position=lambda_.StartingPosition.TRIM_HORIZON,
                batch_size=_BATCH_SIZE,
                report_batch_item_failures=True,
                on_failure=event_sources.SqsDlq(self.dead_letters),
                filters=[
                    lambda_.FilterCriteria.filter({"dynamodb": {"NewImage": {"type": {"S": ["entry"]}}}}),
                    lambda_.FilterCriteria.filter({"dynamodb": {"OldImage": {"type": {"S": ["entry"]}}}}),
                ],
            )
        )
        nag.acknowledge(
            self.function,
            {nag.LAMBDA_DLQ: "Invoked by its stream's event source mapping, whose on-failure destination is a queue"},
        )
        self.alarms = {
            f"{prefix}-ledger-dead-letters": dead_letter_alarm(
                self,
                "DeadLettersAlarm",
                queue=self.dead_letters,
                name=f"{prefix}-ledger-dead-letters",
                description="Ledger entries the writer gave up on: they are in the table but not chained or archived. "
                "Redrive them to the writer",
                topic=topic,
            )
        }
        for what, metric, description in (
            (
                "changed",
                "LedgerEntriesChanged",
                "A ledger entry was changed or removed after it was appended (its first version is chained): find "
                "who wrote around the conditional put",
            ),
            (
                "rejected",
                "LedgerEntriesRejected",
                "An item that is not a valid ledger entry was appended; it is not chained",
            ),
        ):
            name = f"{prefix}-ledger-entries-{what}"
            self.alarms[name] = alarm(
                self,
                f"Entries{what.title()}Alarm",
                name=name,
                metric=cw.Metric(
                    namespace=METRICS_NAMESPACE,
                    metric_name=metric,
                    dimensions_map={"service": _WRITER_SERVICE},
                    statistic=cw.Stats.SUM,
                    period=PERIOD,
                ),
                threshold=1,
                description=description,
                topic=topic,
            )
        self.alarms[f"{prefix}-ledger-lag"] = alarm(
            self,
            "LagAlarm",
            name=f"{prefix}-ledger-lag",
            metric=self.function.function.metric("IteratorAge", statistic=cw.Stats.MAXIMUM, period=PERIOD),
            threshold=MAX_LAG.to_milliseconds(),
            description=f"The ledger archive is more than {MAX_LAG.to_minutes():g} minutes behind its table",
            topic=topic,
        )
