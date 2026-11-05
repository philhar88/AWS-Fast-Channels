"""Names of the agentic-selling module's resources, shared by the stacks (``infra``), the deploy script
(``scripts/_demand.py``), the sync function and the end-to-end tests, so every side agrees."""

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.tags import tag_key

MODULE: Final = "selling"
MODULE_TAG: Final = tag_key("module")
"""``fast:module=selling`` marks what the module deploys (inventory)."""
CONTEXT: Final = "fast-channels:selling"
"""CDK context key (set by ``scripts/prebid_guidance.py deploy``) with the module's :class:`SellingContext`: the Selling
stack is synthesized only with it, beside the guidance."""
BOOKED_LINES_KEY: Final = "lines/booked.json"
"""The booked buys' lines in the PDS config bucket, which the snapshot publisher adds to the own book."""
TENANT: Final = "fast"
"""The seller's tenant: the stage's inventory."""
BUYER_PRINCIPAL: Final = "reference-buyer"
"""The seller's principal (advertiser account) of the AWS reference buyer."""
SELLER_PORT: Final = 8000
"""The seller's nginx port in its container (MCP ``/mcp/``, A2A ``/a2a``, ``/health``)."""


class Image(StrEnum):
    """The module's container images, each built from pinned upstream code (``vendor/agentic-selling/PIN.toml``)."""

    SELLER = "seller"
    """The Prebid Sales Agent with the repository's bridge (``vendor/agentic-selling/seller``)."""
    BUYER = "buyer"
    """The AWS reference buyer's A2A runtime, unmodified."""
    GOVERNANCE = "governance"
    """The AWS reference governance agent, unmodified."""


class SellingContext(BaseModel):
    """What the deploy script passes the Selling stack (:data:`CONTEXT`): the images it built, and what only a deployed
    stage knows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    images: dict[Image, str] = Field(description="Each image's URI, ``<repository>:<content digest>``.")
    catalog_table: str = Field(min_length=3, description="The Data stack's catalog table (the products' genres).")
    origin_facing_prefix_list: str = Field(
        pattern=r"^pl-[0-9a-f]+$",
        description="CloudFront's origin-facing managed prefix list, which the seller's load balancer admits.",
    )


def image_repository(prefix: str, image: Image) -> str:
    """The ECR repository of one of the module's images (the DemandBuild stack's)."""
    return f"{prefix}-{image.value}"


def image_build_project(prefix: str) -> str:
    """The arm64 CodeBuild project that builds the module's images (AgentCore Runtime runs arm64 only)."""
    return f"{prefix}-selling-image"


def seller(prefix: str) -> str:
    """The seller's cluster, service, load balancer, database and log group: ``<prefix>-seller``."""
    return f"{prefix}-seller"


def runtime(prefix: str, image: Image) -> str:
    """An agent's AgentCore runtime (``[a-zA-Z][a-zA-Z0-9_]{0,47}``): ``<prefix>_buyer``, ``<prefix>_governance``."""
    return f"{prefix}_{image.value}"


def buyer_sessions_table(prefix: str) -> str:
    """The reference buyer's sessions table, which its A2A runtime requires (W-10 item 2)."""
    return f"{prefix}-buyer-sessions"


def governance_state_table(prefix: str) -> str:
    """The governance agent's state table (plans, checks, outcomes, the spend aggregate)."""
    return f"{prefix}-governance-state"


def governance_signing_alias(prefix: str) -> str:
    """The KMS alias of the governance agent's ES256 signing key."""
    return f"alias/{prefix}-governance-signing"


def sync_function(prefix: str) -> str:
    """The sync function: booked buys to own-book lines, products from the catalog, delivery for the seller."""
    return f"{prefix}-selling-sync"


def buyer_identity(prefix: str) -> str:
    """The reference buyer's identity in governance checks (``AGENTS_JSON``; never dialled): its runtime URL is known
    only after the runtime exists, which its own environment cannot name."""
    return f"https://buyer.{prefix}.fast-channels.invalid/a2a"


def governance_issuer(prefix: str) -> str:
    """The issuer of the governance agent's tokens (``GOVERNANCE_AGENT_URL``; never dialled), for the same reason."""
    return f"https://governance.{prefix}.fast-channels.invalid/mcp"
