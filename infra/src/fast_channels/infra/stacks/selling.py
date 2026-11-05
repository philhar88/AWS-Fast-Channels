"""``FastChannels-<prefix>-Selling``: agentic selling (``modules.selling``; docs/selling.md, ADR-0024).

The Prebid Sales Agent as the stage's AdCP seller of record, the AWS reference buyer and governance agents, and the
sync function that turns booked buys into own-book lines. It is deployed beside the guidance, in its VPC, after the
Pds and Release stacks (``scripts/prebid_guidance.py deploy``), from the images the deploy built
(:class:`~fast_channels.core.selling.names.SellingContext`).
"""

from typing import TYPE_CHECKING, Any, Final

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm

from fast_channels.core.pds import names as pds_names
from fast_channels.core.selling import names
from fast_channels.core.selling.products import Terms
from fast_channels.core.timeline import BREAK_SLATE_GOPS
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.pds.delivery import allow_table
from fast_channels.infra.constructs.selling.agents import Agents
from fast_channels.infra.constructs.selling.seller import Seller

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.core.demand import DemandNetwork
    from fast_channels.infra.config import FastChannelsConfig

SYNC_HANDLER: Final = "fast_channels.functions.selling.sync.handler"


def terms(config: FastChannelsConfig) -> Terms:
    """What the stage sells: its monetized channels and their breaks, at the configured CPM."""
    selling = config.modules.selling
    grid = config.ladder(config.ladders.default).grid
    return Terms(
        channels=config.modules.release.channels,
        breaks_ms=tuple(grid.gop_exact(gops) for gops in BREAK_SLATE_GOPS),
        cpm=selling.cpm,
        publisher_domain=selling.publisher_domain,
        creatives=selling.creatives,
    )


class SellingStack(Stack):
    """The seller, the buyer and governance agents, and the sync function.

    Args:
        config: deployment configuration (``modules.selling``, ``modules.release.channels``, log retention).
        network: the guidance's network (the seller runs in its private subnets).
        context: the images and what only the deployed stage knows.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        network: DemandNetwork,
        context: names.SellingContext,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): agentic selling", **kwargs)
        Tags.of(self).add(names.MODULE_TAG, names.MODULE)
        prefix, selling = config.prefix, config.modules.selling
        retention = config.observability.log_retention
        vpc = ec2.Vpc.from_vpc_attributes(
            self,
            "GuidanceVpc",
            vpc_id=network.vpc_id,
            availability_zones=[subnet.az for subnet in network.private_subnets],
            private_subnet_ids=[subnet.id for subnet in network.private_subnets],
            private_subnet_route_table_ids=[subnet.route_table for subnet in network.private_subnets],
        )
        repositories: dict[names.Image, ecr.IRepository] = {
            image: ecr.Repository.from_repository_name(
                self, f"{image.value.capitalize()}Repository", names.image_repository(prefix, image)
            )
            for image in names.Image
        }
        tags = {image: context.images[image].rpartition(":")[2] for image in names.Image}
        self.sync = self._sync(config, context)
        self.seller = Seller(
            self,
            "Seller",
            prefix=prefix,
            vpc=vpc,
            repository=repositories[names.Image.SELLER],
            tag=tags[names.Image.SELLER],
            sync_function=self.sync.function,
            origin_facing_prefix_list=context.origin_facing_prefix_list,
            publisher_domain=selling.publisher_domain,
            log_retention=retention,
        )
        seller_url = f"https://{self.seller.distribution.distribution_domain_name}"
        self.agents = Agents(
            self,
            "Agents",
            prefix=prefix,
            repositories=repositories,
            tags=tags,
            seller_url=seller_url,
            seller_token=self.seller.buyer_token,
            model=selling.buyer_model,
            brand=selling.buyer_brand,
            log_retention=retention,
        )
        outputs = {
            "SellerUrl": (seller_url, "The AdCP seller (MCP /mcp/, A2A /a2a, /.well-known/agent-card.json)"),
            "BuyerTokenSecret": (
                self.seller.buyer_token.secret_arn,
                "The reference buyer's bearer token at the seller",
            ),
            "BuyerRuntimeArn": (self.agents.buyer.agent_runtime_arn, "The AWS reference buyer (A2A, SigV4 inbound)"),
            "GovernanceRuntimeArn": (self.agents.governance.agent_runtime_arn, "The reference governance agent (MCP)"),
            "GovernanceUrl": (self.agents.governance_url, "The governance agent's MCP endpoint (Cognito JWT inbound)"),
            "BuyerSessionsTable": (self.agents.sessions.table_name, "The buyer's recorded tool calls"),
            "SyncFunction": (self.sync.function.function_name, "Booked buys to own-book lines, delivery back"),
            "SellerLogGroup": (names.seller(prefix), "The seller's and its bridge's container logs"),
        }
        for key, (value, description) in outputs.items():
            CfnOutput(self, key, value=value, description=description)

    def _sync(self, config: FastChannelsConfig, context: names.SellingContext) -> FastFunction:
        """The sync function the seller's bridge invokes (``functions.selling.sync``)."""
        prefix = config.prefix
        bucket = s3.Bucket.from_bucket_name(
            self,
            "ConfigBucket",
            ssm.StringParameter.value_for_string_parameter(
                self, pds_names.parameter_name(prefix, pds_names.Parameter.CONFIG_BUCKET)
            ),
        )
        publisher = ssm.StringParameter.value_for_string_parameter(
            self, pds_names.parameter_name(prefix, pds_names.Published.PUBLISHER_FUNCTION)
        )
        sync = FastFunction(
            self,
            "Sync",
            handler=SYNC_HANDLER,
            description="Selling: booked AdCP buys to own-book lines, products from the catalog, delivery back",
            environment={
                "FAST_PREFIX": prefix,
                "SELLING_PRODUCTS": terms(config).model_dump_json(),
                "CATALOG_TABLE": context.catalog_table,
                "PDS_CONFIG_BUCKET": bucket.bucket_name,
                "PDS_BOOKED_LINES_KEY": names.BOOKED_LINES_KEY,
                "PDS_TABLE": pds_names.table_name(prefix),
                "PDS_PUBLISHER_FUNCTION": publisher,
            },
            timeout=Duration.seconds(30),
            memory_size=256,
            log_retention=config.observability.log_retention,
            removal_policy=RemovalPolicy.DESTROY,
        )
        nag.acknowledge(sync, {nag.LAMBDA_DLQ: "Invoked synchronously by the seller's bridge, which retries"})
        sync.allow(
            ["s3:GetObject", "s3:PutObject"],
            [bucket.arn_for_objects(names.BOOKED_LINES_KEY)],
        )
        # A GetObject of a missing key is a 404 only with ListBucket: the first sync tells "none yet" from "denied".
        sync.allow(["s3:ListBucket"], [bucket.bucket_arn])
        sync.allow(["dynamodb:Scan"], [arn(self, "dynamodb", "table", context.catalog_table)])
        allow_table(sync, prefix, ["BatchGetItem"], ["BUDGET#adcp-*"])
        sync.allow(["lambda:InvokeFunction"], [arn(self, "lambda", "function", publisher, colon=True)])
        return sync
