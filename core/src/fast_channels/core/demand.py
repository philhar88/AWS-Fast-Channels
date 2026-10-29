"""Names of the demand module's resources, shared by the stacks (``infra``) and the deploy script
(``scripts/prebid_guidance.py``), so both sides always agree.

The module spans two CDK apps: the repository's stacks ``DemandEdge`` (the origin rule group and secrets) and
``DemandBuild`` (the image repository and build, the guidance's CloudFormation execution policy), and the AWS Prebid
Server guidance, deployed as its own app onto a CDK bootstrap of its own. The repository reads the guidance only
through the SSM parameters under :func:`parameter_path`.
"""

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.tags import tag_key

MODULE: Final = "demand"
MODULE_TAG: Final = tag_key("module")
"""``fast:module=demand`` marks everything the module deploys, the guidance's stacks included (inventory)."""
QUALIFIER_MAX: Final = 10
"""CDK bootstrap qualifiers are at most 10 characters (the bootstrap template's ``Qualifier`` pattern)."""
ORIGIN_HEADER: Final = "x-fast-origin"
"""The header MediaTailor sends with the stage's origin secret; the WAF rule group requires it on ``/openrtb2/*``."""
ORIGIN_SECRET_SLOTS: Final = ("a", "b")
"""Two origin secrets are valid at once, so a rotation never blocks a POST (DESIGN §8.6)."""


class Parameter(StrEnum):
    """The SSM parameters ``/<prefix>/demand/<name>`` the deploy script writes after the guidance's deployment: the
    contract between the guidance and the repository's later stacks."""

    CLOUDFRONT_DOMAIN = "cloudfront-domain"
    SETTINGS_BUCKET = "settings-bucket"
    STORED_REQUESTS_BUCKET = "stored-requests-bucket"
    CLUSTER_ARN = "cluster-arn"
    SERVICE_ARN = "service-arn"
    TASK_SECURITY_GROUP = "task-security-group-id"
    VPC_ID = "vpc-id"
    PRIVATE_SUBNET_IDS = "private-subnet-ids"
    ALB_ARN = "alb-arn"
    WEB_ACL_ARN = "web-acl-arn"
    IMAGE_URI = "image-uri"
    GUIDANCE = "guidance"
    """``<tag>@<commit>`` of the deployed guidance, with its overlays."""


def parameter_path(prefix: str, parameter: Parameter | None = None) -> str:
    """``/<prefix>/demand/`` or ``/<prefix>/demand/<parameter>``."""
    return f"/{prefix}/demand/{parameter.value if parameter else ''}"


def guidance_stack(prefix: str) -> str:
    """The guidance's Prebid Server stack (overlay O1 ``stackPrefix``)."""
    return f"{prefix}-pbs"


def simulator_stack(prefix: str) -> str:
    """The guidance's bidder simulator stack (``demand.simulator: guidance_stack``)."""
    return f"{prefix}-pbs-sim"


def bootstrap_qualifier(prefix: str) -> str:
    """The qualifier of the stage's dedicated CDK bootstrap: the prefix itself.

    Raises:
        ValueError: the prefix is longer than a qualifier may be.
    """
    if len(prefix) > QUALIFIER_MAX:
        raise ValueError(f"the demand module needs a prefix of at most {QUALIFIER_MAX} characters, got {prefix!r}")
    return prefix


def toolkit_stack(prefix: str) -> str:
    """The stack of the stage's dedicated CDK bootstrap."""
    return f"{prefix}-cdk-bootstrap"


def assets_bucket(prefix: str, account: str, region: str) -> str:
    """The dedicated bootstrap's file assets bucket; also holds the image build's sources (``pbs-image/``)."""
    return f"{prefix}-cdk-assets-{account}-{region}"


def images_repository(prefix: str, account: str, region: str) -> str:
    """The dedicated bootstrap's container assets repository."""
    return f"{prefix}-cdk-images-{account}-{region}"


def execution_policy(prefix: str) -> str:
    """The managed policy of the dedicated bootstrap's CloudFormation execution role."""
    return f"{prefix}-guidance-cfn-exec"


def guidance_roles(prefix: str) -> str:
    """Names of every IAM role the guidance's stacks create (CloudFormation names them after the stack)."""
    return f"{guidance_stack(prefix)}-*"


def origin_secret(prefix: str, slot: str) -> str:
    """One of the two origin secrets (:data:`ORIGIN_SECRET_SLOTS`)."""
    return f"{prefix}/demand/origin-{slot}"


def origin_rule_group(prefix: str) -> str:
    """The stage's WAF rule group, referenced first by the guidance's web ACL (overlay O2)."""
    return f"{prefix}-demand-origin"


def image_repository(prefix: str) -> str:
    """The ECR repository of the stage's Prebid Server images."""
    return f"{prefix}-pbs"


def image_build_project(prefix: str) -> str:
    """The x86 CodeBuild project that builds them."""
    return f"{prefix}-pbs-image"


IMAGE_SOURCE_PREFIX: Final = "pbs-image/"
"""Key prefix of the build contexts in the assets bucket."""
SIMULATOR_CODE_PREFIX: Final = "simulator/"
"""Key prefix of the ``lambda`` simulator's code packages in the assets bucket."""


def simulator_function(prefix: str) -> str:
    """The bidder simulator's function in ``lambda`` mode (not ``<prefix>-pbs*``: those names are the guidance's)."""
    return f"{prefix}-demand-simulator"


NETWORK_CONTEXT: Final = "fast-channels:demand-network"
"""CDK context with the guidance's network (:class:`DemandNetwork`, JSON), which ``scripts/prebid_guidance.py``
passes once the guidance is deployed; the stage's ``Demand`` stack exists only with it."""


class Subnet(BaseModel):
    """A private subnet of the guidance's VPC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^subnet-[0-9a-f]+$")
    az: str = Field(pattern=r"^[a-z]{2}(-[a-z]+)+-\d[a-z]$")
    route_table: str = Field(pattern=r"^rtb-[0-9a-f]+$")


class DemandNetwork(BaseModel):
    """The guidance as the deploy script reads it from ``/<prefix>/demand/*`` and EC2: its network, its Prebid Server
    service and its CloudFront domain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vpc_id: str = Field(pattern=r"^vpc-[0-9a-f]+$")
    private_subnets: tuple[Subnet, ...] = Field(min_length=2)
    task_security_group_id: str = Field(pattern=r"^sg-[0-9a-f]+$")
    cluster_arn: str = Field(pattern=r"^arn:aws[a-z-]*:ecs:[a-z0-9-]+:\d{12}:cluster/[A-Za-z0-9_-]+$")
    service_arn: str = Field(pattern=r"^arn:aws[a-z-]*:ecs:[a-z0-9-]+:\d{12}:service/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+$")
    cloudfront_domain: str = Field(pattern=r"^[a-z0-9]+\.cloudfront\.net$")
    target_group_arn: str = Field(
        pattern=r"^arn:aws[a-z-]*:elasticloadbalancing:[a-z0-9-]+:\d{12}:targetgroup/[A-Za-z0-9-]+/[0-9a-f]+$",
        description="The guidance's Prebid Server target group (the ALB's IP target group).",
    )
    simulator_code_key: str | None = Field(default=None, pattern=rf"^{SIMULATOR_CODE_PREFIX}[0-9a-f]{{16}}\.zip$")
    """Key of the ``lambda`` simulator's code package in the guidance's assets bucket."""
    container_log_group: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_./#-]{1,512}$")
    """Prebid Server's container log group (the guidance's task definition), where the fast-ctv module logs."""


def census_gate(prefix: str) -> str:
    """The state machine that scales Prebid Server with the stage's running channels."""
    return f"{prefix}-demand-census-gate"


def warmup_project(prefix: str) -> str:
    """The CodeBuild project that replays synthetic auctions against new Prebid Server tasks."""
    return f"{prefix}-pbs-warmup"


def daypart_action(prefix: str, index: int) -> str:
    """The Application Auto Scaling scheduled action of the stage's ``index``-th daypart."""
    return f"{prefix}-daypart-{index}"


SCALE_IN_AFTER_SECONDS: Final = 900
"""How long no channel must run before Prebid Server scales to zero (15 minutes: a stop and restart keeps it)."""
VEGETA: Final = {
    "version": "12.13.0",
    "url": "https://github.com/tsenart/vegeta/releases/download/v12.13.0/vegeta_12.13.0_linux_arm64.tar.gz",
    "sha256": "950381173a5575e25e8e086f36fc03bf65d61a2433329b48e41e1cb5e4133bba",
}
"""The warm-up's load generator, pinned and checksum-verified (``vegeta_12.13.0_checksums.txt`` of the release)."""
WARMUP_RATE_PER_TASK: Final = 50
"""Synthetic auctions per second per healthy target when a new task's warm-up starts (P0-21: 50-100/s per task for
2-3 minutes warms a JVM's JIT). The replay goes through the load balancer, which spreads it over every healthy target,
so the new task gets about this rate."""
WARMUP_HEALTHY_WAIT_SECONDS: Final = 600
"""How long a warm-up waits for its task to become a healthy target before it gives up (and fails)."""
WARMUP_SECONDS: Final = 150
