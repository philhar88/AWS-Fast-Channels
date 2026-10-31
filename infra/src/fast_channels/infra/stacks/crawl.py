"""``FastChannels-Crawl``: show-level content metadata for ad requests (``modules.crawl``; ADR 0031)."""

from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_sns as sns

from fast_channels.core.crawl.catalog_context import CONTEXT_PREFIX
from fast_channels.infra.constructs.alerts import alert_topic_name
from fast_channels.infra.constructs.crawl.publisher import ContextPublisher

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig
    from fast_channels.infra.exports import DataExports, MediaExports


class CrawlStack(Stack):
    """The catalog context publisher, deployed when ``modules.crawl.enabled``. It writes one object per catalog
    title to ``catalog/ctx/v1/`` in the catalog-context bucket, which the Media stack creates with the module and
    serves on the CDN's ``/catalog/ctx/*`` route; the Release Controller's PRE_ADS lookup and the Pod Decision
    Service read those objects.

    Args:
        config: deployment configuration (``modules.crawl``, ``observability``, ``removal``).
        data: Data stack references (the catalog and media library tables).
        media: Media stack exports (the bus, the catalog-context bucket and the CDN base URL). The alert topic is
            imported by its name.

    Raises:
        ValueError: ``media`` has no catalog-context bucket (the Media stack was built with the module off).

    Attributes:
        publisher: the context publisher.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        data: DataExports,
        media: MediaExports,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            scope, id, description=f"FAST Channels ({config.prefix}): show-level content metadata", **kwargs
        )
        if media.catalog_context_bucket is None:
            raise ValueError("the Crawl stack needs the Media stack's catalog-context bucket (modules.crawl on)")
        self.publisher = ContextPublisher(
            self,
            "Publisher",
            prefix=config.prefix,
            data=data,
            bucket=media.catalog_context_bucket,
            event_bus=media.event_bus,
            alert_topic=sns.Topic.from_topic_arn(
                self, "Alerts", self.format_arn(service="sns", resource=alert_topic_name(config.prefix))
            ),
            publisher_domain=config.modules.crawl.publisher_domain,
            log_retention=config.observability.log_retention,
            removal_policy=config.removal_policy,
        )
        CfnOutput(
            self,
            "CatalogContextBaseUrl",
            value=f"{media.cdn_base_url}/{CONTEXT_PREFIX}",
            description="Base URL of the catalog context objects (<base>/<content_id>.json)",
        )
        CfnOutput(
            self,
            "CatalogContextPublisherName",
            value=self.publisher.function.function.function_name,
            description='Context publisher; invoke it with {"sweep": true} to publish the whole catalog now',
        )
        CfnOutput(
            self,
            "CatalogContextBucketName",
            value=media.catalog_context_bucket.bucket_name,
            description="Bucket of the catalog context objects (catalog/ctx/v1/<content_id>.json)",
        )
