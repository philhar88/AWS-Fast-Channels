"""Names of the demand module's resources, shared by the stacks (``infra``) and the deploy script
(``scripts/prebid_guidance.py``), so both sides always agree.

The module spans two CDK apps: the repository's stacks ``DemandEdge`` (the origin rule group and secrets) and
``DemandBuild`` (the image repository and build, the guidance's CloudFormation execution policy), and the AWS Prebid
Server guidance, deployed as its own app onto a CDK bootstrap of its own. The repository reads the guidance only
through the SSM parameters under :func:`parameter_path`.
"""

from enum import StrEnum
from typing import Final

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
