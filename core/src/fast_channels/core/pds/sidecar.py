"""What the Prebid Server task gets for the Pod Decision Service's sidecar (ADR-0036): its container, the task size and
the task role's grants, as the CDK context ``scripts/guidance_app.py`` reads (:data:`CONTEXT`).

``scripts/_demand.py`` renders it at deploy (:func:`sidecar`); the wrapper adds it to the guidance's own stack. The
grants are exactly the hot-path functions' were: the snapshot object, the budget shards' reads, the reservations'
writes (``BUDGET#``, ``CAP#``, ``RES#`` items) and the ledger's ``pds/*`` chains, and with the crawl module the
catalog-context objects. The task role is shared with Prebid Server's container, so Prebid Server could make these
calls too (ADR-0036 weighs that).

The sidecar runs in the guidance's VPC, whose S3 gateway endpoint lets requests through only to the guidance's own
buckets (its endpoint policy): S3 answers anything else ``AccessDenied``, whatever the role allows (seen live). So
the same S3 reads also join that endpoint's policy (:func:`endpoint_statements`), for any principal of the account
and those objects only.
"""

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.crawl.catalog_context import CONTEXT_PREFIX
from fast_channels.core.ledger import ledger_table
from fast_channels.core.pds.names import CONTAINER, PORT, sidecar_log_group, table_name
from fast_channels.core.pds.snapshot import SNAPSHOT_KEY

CONTEXT: Final = "fastPds"
"""The CDK context key the guidance wrapper reads the sidecar from (JSON)."""
SERVICE: Final = "pds"
"""``POWERTOOLS_SERVICE_NAME`` of the sidecar's logs and metrics."""
TASK_CPU: Final = 2048
"""The task's CPU units with the sidecar: the guidance's 2 vCPU, which both containers share."""
TASK_MEMORY_MIB: Final = 6144
"""The task's memory with the sidecar: the guidance's 4 GiB plus 2 GiB. Prebid Server's JVM sizes its heap from the
task's memory (a quarter by default), and the sidecar needs a few hundred MiB."""
HEALTH_COMMAND: Final = (
    "CMD",
    "python",
    "-c",
    f"import urllib.request as u; u.urlopen('http://127.0.0.1:{PORT}/health', timeout=2)",
)
"""The container's health check: the image has Python and no curl."""


class Sidecar(BaseModel):
    """The sidecar as the guidance wrapper adds it (module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str = Field(min_length=1, description="The sidecar's image URI.")
    container: str = CONTAINER
    port: int = PORT
    environment: dict[str, str]
    log_group: str
    log_retention_days: int = Field(gt=0)
    health_command: tuple[str, ...] = HEALTH_COMMAND
    task_cpu: int = TASK_CPU
    task_memory_mib: int = TASK_MEMORY_MIB
    statements: tuple[dict[str, Any], ...] = Field(description="IAM statements added to the task role.")
    endpoint_statements: tuple[dict[str, Any], ...] = Field(
        description="Statements added to the policy of the guidance VPC's S3 gateway endpoint."
    )


def _table(partition: str, region: str, account: str, name: str) -> str:
    return f"arn:{partition}:dynamodb:{region}:{account}:table/{name}"


def _leading(keys: list[str]) -> dict[str, Any]:
    return {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": keys}}


def statements(
    *,
    prefix: str,
    partition: str,
    region: str,
    account: str,
    config_bucket: str,
    catalog_context_bucket: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """The sidecar's grants (module docstring)."""
    pds = _table(partition, region, account, table_name(prefix))
    ledger = _table(partition, region, account, ledger_table(prefix))
    granted: list[dict[str, Any]] = [
        {
            "Sid": "PdsSnapshot",
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": f"arn:{partition}:s3:::{config_bucket}/{SNAPSHOT_KEY}",
        },
        {
            "Sid": "PdsBudgetReads",
            "Effect": "Allow",
            "Action": "dynamodb:BatchGetItem",
            "Resource": pds,
            "Condition": _leading(["BUDGET#*"]),
        },
        {
            "Sid": "PdsReservations",
            "Effect": "Allow",
            "Action": "dynamodb:UpdateItem",
            "Resource": pds,
            "Condition": _leading(["BUDGET#*", "CAP#*"]),
        },
        {
            "Sid": "PdsReservationItems",
            "Effect": "Allow",
            "Action": "dynamodb:PutItem",
            "Resource": pds,
            "Condition": _leading(["RES#*"]),
        },
        {
            "Sid": "PdsLedger",
            "Effect": "Allow",
            "Action": "dynamodb:PutItem",
            "Resource": ledger,
            "Condition": _leading(["pds/*"]),
        },
    ]
    if catalog_context_bucket is not None:
        granted += [
            {
                "Sid": "PdsCatalogContextList",
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": f"arn:{partition}:s3:::{catalog_context_bucket}",
                "Condition": {"StringEquals": {"s3:prefix": CONTEXT_PREFIX}},
            },
            {
                "Sid": "PdsCatalogContexts",
                "Effect": "Allow",
                "Action": "s3:GetObject",
                "Resource": f"arn:{partition}:s3:::{catalog_context_bucket}/{CONTEXT_PREFIX}*",
            },
        ]
    return tuple(granted)


def endpoint_statements(
    *,
    account: str,
    partition: str,
    config_bucket: str,
    catalog_context_bucket: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """The sidecar's S3 reads in the guidance VPC's gateway endpoint policy (module docstring), as the guidance's own
    statements are: any principal, the account's resources only."""
    own = {"StringEquals": {"aws:ResourceAccount": account}}
    reads: list[dict[str, Any]] = [
        {
            "Sid": "PdsSnapshot",
            "Effect": "Allow",
            "Principal": {"AWS": "*"},
            "Action": "s3:GetObject",
            "Resource": f"arn:{partition}:s3:::{config_bucket}/{SNAPSHOT_KEY}",
            "Condition": own,
        }
    ]
    if catalog_context_bucket is not None:
        reads.append(
            {
                "Sid": "PdsCatalogContexts",
                "Effect": "Allow",
                "Principal": {"AWS": "*"},
                "Action": ["s3:ListBucket", "s3:GetObject"],
                "Resource": [
                    f"arn:{partition}:s3:::{catalog_context_bucket}",
                    f"arn:{partition}:s3:::{catalog_context_bucket}/{CONTEXT_PREFIX}*",
                ],
                "Condition": own,
            }
        )
    return tuple(reads)


def sidecar(
    *,
    prefix: str,
    image: str,
    partition: str,
    region: str,
    account: str,
    config_bucket: str,
    content_base_url: str,
    log_retention_days: int,
    crawl: dict[str, str] | None = None,
    catalog_context_bucket: str | None = None,
) -> Sidecar:
    """The sidecar of stage ``prefix``.

    Args:
        prefix: the stage's prefix.
        image: the sidecar's image URI.
        partition, region, account: where the stage is (its grants' ARNs).
        config_bucket: the PDS config bucket (the snapshot).
        content_base_url: the CDN's content base URL, our tracker's host.
        log_retention_days: the sidecar's log retention (``observability.log_retention_days``).
        crawl: with the crawl module on, its environment (``CRAWL_HOLDOUT_PERCENT``, ``PBS_VERSION``, and
            ``PUBLISHER_DOMAIN`` when set).
        catalog_context_bucket: with the crawl module on, its catalog-context bucket.
    """
    environment = {
        "FAST_PREFIX": prefix,
        "POWERTOOLS_SERVICE_NAME": SERVICE,
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,  # botocore's
        "PDS_CONFIG_BUCKET": config_bucket,
        "PDS_TABLE": table_name(prefix),
        "LEDGER_TABLE": ledger_table(prefix),
        "CONTENT_BASE_URL": content_base_url,
        **(crawl or {}),
        **({"CATALOG_CONTEXT_BUCKET": catalog_context_bucket} if catalog_context_bucket else {}),
    }
    return Sidecar(
        image=image,
        environment=environment,
        log_group=sidecar_log_group(prefix),
        log_retention_days=log_retention_days,
        statements=statements(
            prefix=prefix,
            partition=partition,
            region=region,
            account=account,
            config_bucket=config_bucket,
            catalog_context_bucket=catalog_context_bucket,
        ),
        endpoint_statements=endpoint_statements(
            account=account,
            partition=partition,
            config_bucket=config_bucket,
            catalog_context_bucket=catalog_context_bucket,
        ),
    )
