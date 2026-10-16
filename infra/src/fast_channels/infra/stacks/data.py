"""``FastChannels-Data``: every stateful resource of a deployment, deployed first and protected in production."""

from dataclasses import dataclass
from typing import Any, Final, cast

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from fast_channels.core.naming import agentcore_name
from fast_channels.infra.config import FastChannelsConfig, Removal
from fast_channels.infra.constructs.storage import GSI1, MediaLibrary, MediaStorage, ReferenceCatalogTable

MEMORY_EVENT_EXPIRY_DAYS: Final = 30


@dataclass(frozen=True, slots=True)
class DataExports:
    """References to DataStack's resources, for the consumer stacks (Media, Operator, Edge).

    They are imported from L1 attribute tokens, which guarantees two things. Consumers get weak
    ``Fn::GetStackOutput`` references, which also work across regions (EdgeStack runs in us-east-1). And nothing a
    consumer does (grants, ``S3BucketOrigin.with_origin_access_control``) can add a statement to a DataStack
    resource policy, so no cross-stack cycle can form; grants land on the consumer's own principals.
    """

    ingest_bucket: s3.IBucket
    media_bucket: s3.IBucket
    logs_bucket: s3.IBucket
    library_table: dynamodb.ITableV2
    catalog_table: dynamodb.ITableV2
    memory: agentcore.IMemory | None
    """AgentCore Memory of the scheduler agent; ``None`` when ``agent.enabled`` is false."""


class DataStack(Stack):
    """Ingest, media and logs buckets with their single bucket policies, the media library and reference catalog
    tables, and the agent's AgentCore Memory.

    Memory lives here, not in the Operator stack, so an Operator rollback can never get stuck on a Memory that is
    still ``CREATING`` (agentcore.md §12, gotcha 5). With ``removal: retain`` everything is retained, tables get
    deletion protection and the stage turns on termination protection.

    Attributes:
        exports: what consumer stacks receive (see :class:`DataExports`).
    """

    def __init__(self, scope: Construct, id: str, *, config: FastChannelsConfig, **kwargs: Any) -> None:
        super().__init__(
            scope, id, description=f"FAST Channels ({config.prefix}): media storage, library and agent memory", **kwargs
        )
        protect = config.removal is Removal.RETAIN
        self.storage = MediaStorage(
            self,
            "Storage",
            removal_policy=config.removal_policy,
            log_expiration=Duration.days(config.observability.log_retention_days),
        )
        self.storage.allow_cloudfront_read()
        self.storage.allow_channel_assembly_read(config.prefix)
        self.storage.allow_log_delivery()
        self.library = MediaLibrary(self, "Library", removal_policy=config.removal_policy, deletion_protection=protect)
        self.catalog = ReferenceCatalogTable(
            self, "Catalog", removal_policy=config.removal_policy, deletion_protection=protect
        )
        self.memory: agentcore.Memory | None = None
        if config.agent.enabled:
            self.memory = agentcore.Memory(
                self,
                "Memory",
                memory_name=agentcore_name(config.prefix, "memory"),
                description="Scheduler agent: operator conversations and their scheduling preferences",
                expiration_duration=Duration.days(MEMORY_EVENT_EXPIRY_DAYS),
                # USER_PREFERENCE only: semantic extraction would turn catalog text into "facts" (_review.md,
                # agent lens).
                memory_strategies=[agentcore.MemoryStrategy.using_built_in_user_preference()],
            )
            # The L2's own apply_removal_policy fails: its CfnMemory child is not named "Resource".
            _cfn_memory(self.memory).apply_removal_policy(config.removal_policy)
        self.exports = self._exports()
        self._outputs()

    def _exports(self) -> DataExports:
        scope = Construct(self, "Exports")

        def bucket(id: str, bucket: s3.Bucket) -> s3.IBucket:
            cfn = cast("s3.CfnBucket", bucket.node.default_child)
            return s3.Bucket.from_bucket_attributes(scope, id, bucket_arn=cfn.attr_arn, bucket_name=cfn.ref)

        def table(id: str, table: dynamodb.TableV2) -> dynamodb.ITableV2:
            cfn = cast("dynamodb.CfnGlobalTable", table.node.default_child)
            return dynamodb.TableV2.from_table_attributes(scope, id, table_name=cfn.ref, global_indexes=[GSI1])

        memory = None
        if self.memory is not None:
            cfn_role = cast("iam.CfnRole", cast("iam.Role", self.memory.execution_role).node.default_child)
            memory = agentcore.Memory.from_memory_attributes(
                scope, "Memory", memory_arn=_cfn_memory(self.memory).attr_memory_arn, role_arn=cfn_role.attr_arn
            )
        return DataExports(
            ingest_bucket=bucket("IngestBucket", self.storage.ingest_bucket),
            media_bucket=bucket("MediaBucket", self.storage.media_bucket),
            logs_bucket=bucket("LogsBucket", self.storage.logs_bucket),
            library_table=table("LibraryTable", self.library),
            catalog_table=table("CatalogTable", self.catalog),
            memory=memory,
        )

    def _outputs(self) -> None:
        """Human-readable outputs for scripts (seed, trace, e2e); stacks consume :attr:`exports` instead."""
        outputs = {
            "IngestBucketName": self.storage.ingest_bucket.bucket_name,
            "MediaBucketName": self.storage.media_bucket.bucket_name,
            "LogsBucketName": self.storage.logs_bucket.bucket_name,
            "LibraryTableName": self.library.table_name,
            "CatalogTableName": self.catalog.table_name,
        }
        if self.memory is not None:
            outputs["MemoryId"] = self.memory.memory_id
        for name, value in outputs.items():
            CfnOutput(self, name, value=value)


def _cfn_memory(memory: agentcore.Memory) -> agentcore.CfnMemory:
    return cast("agentcore.CfnMemory", memory.node.find_child("Memory"))
