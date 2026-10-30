"""Deployment configuration: the ``fast-channels`` block of the CDK context, validated by pydantic.

The block lives in ``cdk.json``. Override any field at synth time with ``-c fast-channels.<path>=<value>``; values
are parsed as JSON when they can be (``-c fast-channels.agent.enabled=false``,
``-c fast-channels.channels.iac='[{"name": "fast-news"}]'``), list items are addressed by index
(``-c fast-channels.channels.iac.0.tier=STANDARD``) and JSON objects are deep-merged. ``-c fast-channels='<json>'``
replaces the whole block (CDK CLI context semantics); fields it omits take the defaults below.

Every attribute docstring below is the field's documentation (also exported in the JSON schema).
"""

import json
import os
import re
from decimal import Decimal
from enum import StrEnum
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Self

from aws_cdk import RemovalPolicy
from aws_cdk import aws_logs as logs
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from fast_channels.core.demand import QUALIFIER_MAX
from fast_channels.core.demo_demand import DemoRole, crids
from fast_channels.core.ladder import Ladder
from fast_channels.core.model_registry import SCHEDULER_TASK, TASK_NAME, TaskSettings, qualification
from fast_channels.core.monetization.names import sibling_pattern
from fast_channels.core.naming import PREFIX_PATTERN, agent_namespace, check_prefix, is_agent_owned
from fast_channels.core.profiles import CATALOGUE, DEFAULT_PACKAGE_PROFILES
from fast_channels.core.specs import validate_ads_url
from fast_channels.core.timeline import ProgrammingBrief  # noqa: TC001 - pydantic resolves it at runtime

if TYPE_CHECKING:
    from constructs import Node

CONTEXT_KEY: Final = "fast-channels"
GEO_PLACEHOLDER: Final = "{geo}"
"""Stands for the deployment region's inference-profile geography in model ids (:func:`geo_prefix`)."""
DEFAULT_MODEL: Final = f"{GEO_PLACEHOLDER}.anthropic.claude-haiku-5-5"
EVAL_RECORDS: Final = Path(__file__).resolve().parents[4] / "evals" / "records"
"""The committed eval records (``evals/records/<task>/<key>.json``) that qualify registry entries."""
EVAL_RECORDS_ENV: Final = "FAST_EVAL_RECORDS"
"""Directories to read eval records from instead (``os.pathsep``-separated, first match wins), e.g. for tests."""
SCHEDULER_EVAL: Final = "haiku-5-5-adaptive-16k"
"""The committed record of the shipped ``scheduler.plan`` entry (``make eval TASK=scheduler.plan``)."""
SCHEDULER_DAILY_CAP_USD: Final = 5
"""The scheduler agent's daily spend cap, about ten times a busy demo day: four autopilot runs a day on each of five
channels and 50 operator turns at about $0.005 each (``make eval``) is about $0.4."""
DEFAULT_ALLOWED_MODELS: Final = (
    DEFAULT_MODEL,
    f"{GEO_PLACEHOLDER}.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-haiku-5-5",
    "global.anthropic.claude-sonnet-5-5",
)
MAX_PACKAGE_PROFILES: Final = 5  # package configurations per VOD source, default quota (channel-assembly.md §3.3)

# MediaTailor (Channel Assembly) ∩ MediaConvert, from the botocore endpoint data (2026-10).
MEDIA_REGIONS: Final = frozenset(
    {
        "af-south-1",
        "ap-northeast-1",
        "ap-northeast-2",
        "ap-northeast-3",
        "ap-south-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-southeast-4",
        "ap-southeast-5",
        "ca-central-1",
        "eu-central-1",
        "eu-north-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "me-central-1",
        "sa-east-1",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
)
# AgentCore Memory: the narrowest of Runtime, Gateway, Policy, Identity and Memory ("Supported AWS Regions", 2026-10).
AGENTCORE_REGIONS: Final = frozenset(
    {
        "ap-northeast-1",
        "ap-northeast-2",
        "ap-south-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "ca-central-1",
        "eu-central-1",
        "eu-north-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "sa-east-1",
        "us-east-1",
        "us-east-2",
        "us-west-2",
    }
)
# Geographic cross-Region inference profile prefix -> region-name prefixes allowed to call it (``global.`` = any).
# ca-central-1 is a source region of the us. profiles (get-inference-profile there lists it among the destinations).
_GEO_PROFILE_REGIONS: Final = {
    "us": ("us-", "ca-central-1"),
    "eu": ("eu-",),
    "apac": ("ap-",),
    "ca": ("ca-",),
    "jp": ("ap-northeast-1", "ap-northeast-3"),
    "au": ("ap-southeast-2", "ap-southeast-4"),
}
# Region-name prefix -> the geography ``{geo}`` stands for there: where a geographic profile of both default models
# (Claude Haiku 5.5 and Sonnet 5.5) exists, from ``aws bedrock list-inference-profiles`` in every AgentCore region
# (2026-10-16). APAC regions have no apac. profile of them and jp. has no Sonnet, so they fall back to global.
_DEFAULT_GEOGRAPHIES: Final = (("us-", "us"), ("ca-central-1", "us"), ("eu-", "eu"), ("ap-southeast-2", "au"))
_RETENTION: Final = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    5: logs.RetentionDays.FIVE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    60: logs.RetentionDays.TWO_MONTHS,
    90: logs.RetentionDays.THREE_MONTHS,
    120: logs.RetentionDays.FOUR_MONTHS,
    150: logs.RetentionDays.FIVE_MONTHS,
    180: logs.RetentionDays.SIX_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
    400: logs.RetentionDays.THIRTEEN_MONTHS,
    545: logs.RetentionDays.EIGHTEEN_MONTHS,
    731: logs.RetentionDays.TWO_YEARS,
    1096: logs.RetentionDays.THREE_YEARS,
    1827: logs.RetentionDays.FIVE_YEARS,
    2192: logs.RetentionDays.SIX_YEARS,
    2557: logs.RetentionDays.SEVEN_YEARS,
    2922: logs.RetentionDays.EIGHT_YEARS,
    3288: logs.RetentionDays.NINE_YEARS,
    3653: logs.RetentionDays.TEN_YEARS,
}
_HTTPS_URL: Final = r"^https://[^\s/]+(/\S*)?$"
_EMAIL: Final = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
_DOMAIN: Final = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9-]+)+$"


class ConfigError(ValueError):
    """The ``fast-channels`` configuration is invalid; the message lists every problem with its context path."""


def geo_prefix(region: str) -> str:
    """The inference-profile geography that :data:`GEO_PLACEHOLDER` stands for in ``region``: ``us`` in the US and
    ca-central-1, ``eu`` in Europe, ``au`` in ap-southeast-2, ``global`` elsewhere."""
    return next((geo for start, geo in _DEFAULT_GEOGRAPHIES if region.startswith(start)), "global")


def resolve_geo(model: str, region: str) -> str:
    """``model`` with :data:`GEO_PLACEHOLDER` replaced for ``region`` (``{geo}.anthropic.claude-haiku-5-5`` ->
    ``eu.anthropic.claude-haiku-5-5`` in eu-west-1)."""
    return model.replace(GEO_PLACEHOLDER, geo_prefix(region))


class Removal(StrEnum):
    """What happens to stateful resources (buckets, tables, Memory, log groups) when a stack is deleted."""

    DESTROY = "destroy"
    RETAIN = "retain"


class ChannelTier(StrEnum):
    """Channel Assembly channel tier (BASIC: VOD sources only; STANDARD adds live sources and time-shift)."""

    BASIC = "BASIC"
    STANDARD = "STANDARD"


class TransactionSearchMode(StrEnum):
    """CloudWatch Transaction Search, required for AgentCore spans, Gateway/Memory traces and evaluations."""

    OFF = "off"
    ENABLE = "enable"
    EXISTING = "existing"


class AsRunMode(StrEnum):
    """How the deployment reads Channel Assembly's account-shared as-run log."""

    PER_STAGE = "per_stage"
    SHARED = "shared"


class PriceClass(StrEnum):
    """CloudFront price class (edge locations the distribution uses)."""

    ALL = "PriceClass_All"
    P200 = "PriceClass_200"
    P100 = "PriceClass_100"


class AdsMode(StrEnum):
    """How ad breaks are filled."""

    PRECONDITIONED = "preconditioned"
    EXTERNAL_VAST = "external_vast"


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)


@cache
def _ladder(name: str) -> Ladder:
    return Ladder.builtin(name)


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    if duplicates := sorted({v for v in values if values.count(v) > 1}):
        raise ValueError(f"duplicate entries: {', '.join(duplicates)}")
    return values


def _known_profiles(names: tuple[str, ...]) -> tuple[str, ...]:
    catalogue = [p.name for p in CATALOGUE]
    if unknown := [name for name in names if name not in catalogue]:
        raise ValueError(f"unknown rendition profiles {unknown}; choose from {catalogue}")
    return _unique(names)


class LaddersConfig(_Section):
    builtin: tuple[str, ...] = ("standard",)
    """Built-in ladders (``ladders/<name>.toml``), each deployed as a MediaConvert job template. Slate and house-ad
    kits are encoded at deploy time for the default ladder and every ladder an IaC channel uses."""
    default: str = "standard"
    """Ladder used when a request names none (ingest, agent proposals); one of ``builtin``."""

    @field_validator("builtin")
    @classmethod
    def _loadable(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        if not names:
            raise ValueError("at least one built-in ladder is required")
        for name in _unique(names):
            try:
                _ladder(name)
            except FileNotFoundError:
                raise ValueError(f"unknown ladder {name!r}: there is no ladders/{name}.toml") from None
        return names

    @model_validator(mode="after")
    def _default_is_builtin(self) -> Self:
        if self.default not in self.builtin:
            raise ValueError(f"default ladder {self.default!r} is not in builtin {list(self.builtin)}")
        return self


class ChannelAssemblyConfig(_Section):
    package_profiles: tuple[str, ...] = DEFAULT_PACKAGE_PROFILES
    """Rendition profiles registered as package configurations on every VOD source and slate (at most 5, the
    default per-source quota). Every channel's ``outputs`` must be a subset."""

    @field_validator("package_profiles")
    @classmethod
    def _valid(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        if not 1 <= len(names) <= MAX_PACKAGE_PROFILES:
            raise ValueError(f"between 1 and {MAX_PACKAGE_PROFILES} profiles are required, got {len(names)}")
        return _known_profiles(names)


class ChannelConfig(_Section):
    name: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", max_length=40)
    """Channel Assembly channel name. Must start with ``<prefix>-``; ``<prefix>-agent-`` is reserved for channels
    the agent creates. ``{prefix}`` is replaced with the deployment prefix (the default is ``{prefix}-demo``). Another
    channel's name plus ``-p-...`` (or ``<prefix>-lab-p-...``) is refused, with the release module on or off: those are
    the release module's sibling names, and its IAM grants cover ``<channel>-p-*``. So is ``<prefix>-agent``, whose
    siblings would be agent-owned names."""
    ladder: str = "standard"
    """Built-in ladder of every program, slate and house ad on this channel (one of ``ladders.builtin``)."""
    outputs: tuple[str, ...] = DEFAULT_PACKAGE_PROFILES
    """Rendition profiles published as channel outputs (subset of ``channel_assembly.package_profiles``)."""
    tier: ChannelTier = ChannelTier.BASIC
    """Channel tier; billed per running hour, and channels are never started automatically."""
    autopilot_cron: str | None = Field(default=None, pattern=r"^(cron|rate)\(.+\)$")
    """EventBridge Scheduler expression for the autopilot that keeps the schedule 24 h ahead, e.g.
    ``cron(0 */6 * * ? *)``. Omit to schedule only by chat."""
    brief: ProgrammingBrief | None = None
    """Programming brief (ratings, genres, dayparts, break policy) enforced by the schedule planner."""

    @field_validator("outputs")
    @classmethod
    def _valid_outputs(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        if not names:
            raise ValueError("at least one output is required")
        return _known_profiles(names)


class ChannelsConfig(_Section):
    iac: tuple[ChannelConfig, ...] = ()
    """Channels owned by CloudFormation (``owner = iac``), deployed with their playback configurations."""
    max_runtime_hours: float | None = Field(default=4, gt=0)
    """Hours after which a channel started by a tool is stopped automatically; ``null`` disables auto-stop."""
    max_agent_channels: int = Field(default=3, ge=0)
    """Maximum number of channels the agent may create (``<prefix>-agent-*``)."""

    @field_validator("iac")
    @classmethod
    def _unique_names(cls, channels: tuple[ChannelConfig, ...]) -> tuple[ChannelConfig, ...]:
        _unique(tuple(c.name for c in channels))
        return channels


class CdnEndpoint(_Section):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    """Short CDN name used in playback-URL listings, e.g. ``cdn-b`` (``cloudfront`` is reserved for the primary)."""
    manifest_base: str = Field(pattern=_HTTPS_URL)
    """Base URL this CDN serves MediaTailor manifests under (chains to CloudFront)."""
    content_base: str = Field(pattern=_HTTPS_URL)
    """Base URL this CDN serves content segments under."""

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, name: str) -> str:
        if name == "cloudfront":
            raise ValueError("'cloudfront' is reserved for the deployment's own distribution")
        return name

    @field_validator("manifest_base", "content_base")
    @classmethod
    def _no_trailing_slash(cls, url: str) -> str:
        return url.rstrip("/")


class DeliveryConfig(_Section):
    content_domain: str | None = Field(default=None, pattern=_DOMAIN)
    """CDN-neutral hostname for content segments and preconditioned ads (e.g. ``media.example.com``). Requires
    ``certificate_arn``; defaults to the CloudFront domain."""
    certificate_arn: str | None = Field(default=None, pattern=r"^arn:aws[a-z-]*:acm:us-east-1:\d{12}:certificate/\S+$")
    """ACM certificate in us-east-1 covering ``content_domain`` (CloudFront only accepts us-east-1 certificates)."""
    price_class: PriceClass = PriceClass.ALL
    """CloudFront price class."""
    cdns: tuple[CdnEndpoint, ...] = ()
    """Additional CDNs that chain to CloudFront; used to list alternative playback URLs."""

    @model_validator(mode="after")
    def _domain_needs_certificate(self) -> Self:
        if (self.content_domain is None) != (self.certificate_arn is None):
            raise ValueError("content_domain and certificate_arn must be set together")
        _unique(tuple(cdn.name for cdn in self.cdns))
        return self


class PreconditionedAds(_Section):
    """House ads encoded on each channel's ladder and served as a static VAST pod: MediaTailor never transcodes."""

    mode: Literal[AdsMode.PRECONDITIONED] = AdsMode.PRECONDITIONED


class ExternalVastAds(_Section):
    """A third-party ad decision server; MediaTailor transcodes its creatives to the channel's variants."""

    mode: Literal[AdsMode.EXTERNAL_VAST]
    url: str = Field(pattern=r"^https?://\S+$")
    """ADS URL; may contain documented MediaTailor dynamic variables such as ``[session.uuid]``."""

    @field_validator("url")
    @classmethod
    def _known_variables(cls, url: str) -> str:
        return validate_ads_url(url)  # unknown variables would silently become empty strings


type AdsConfig = Annotated[PreconditionedAds | ExternalVastAds, Field(discriminator="mode")]


class AgentConfig(_Section):
    enabled: bool = True
    """Deploy the operator plane (Cognito, agent tools, Gateway, AgentCore Runtime and Memory, console)."""
    model: str = Field(default=DEFAULT_MODEL, validate_default=True)
    """Default Bedrock model id or inference profile of the scheduler agent; one of ``allowed_models``. ``{geo}``
    stands for the deployment region's geography: ``us`` (US regions and ca-central-1), ``eu`` (Europe), ``au``
    (ap-southeast-2) or ``global`` (elsewhere)."""
    allowed_models: tuple[str, ...] = Field(default=DEFAULT_ALLOWED_MODELS, validate_default=True)
    """Models the agent may be switched to at runtime (SSM ModelSpec); the runtime role may invoke only these.
    ``{geo}`` as in ``model``; entries that resolve to the same id are kept once."""
    admin_email: str | None = Field(default=None, pattern=_EMAIL)
    """Email of the first ``fast-admins`` user (invited by Cognito). Omit to create users yourself."""
    machine_client: bool = False
    """Create the confidential machine client (Cognito ``client_credentials``) that integrations and the end-to-end
    tests use to call the agent and the Gateway; always created when a channel has an ``autopilot_cron``. Anyone
    allowed ``cognito-idp:DescribeUserPoolClient`` can read its secret, so it is off by default."""
    console_dev_url: str | None = Field(default=None, pattern=r"^http://(localhost|127\.0\.0\.1):\d{2,5}/$")
    """Local console dev server (``http://localhost:5173/``) to accept as a sign-in and sign-out redirect, for
    ``npm run dev`` against this deployment. Off by default: with it, any process listening on that port of an
    operator's machine can receive their authorization codes."""

    @field_validator("model", mode="after")
    @classmethod
    def _resolve_model(cls, model: str, info: ValidationInfo) -> str:
        region = (info.context or {}).get("region")
        return resolve_geo(model, region) if region else model

    @field_validator("allowed_models", mode="after")
    @classmethod
    def _resolve_allowed(cls, models: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        _unique(models)
        region = (info.context or {}).get("region")
        return tuple(dict.fromkeys(resolve_geo(model, region) for model in models)) if region else models

    @model_validator(mode="after")
    def _model_is_allowed(self) -> Self:
        if self.model not in self.allowed_models:
            raise ValueError(f"model {self.model!r} is not in allowed_models {list(self.allowed_models)}")
        return self

    @property
    def unresolved(self) -> bool:
        """Whether a model id still holds ``{geo}``: validated without a region (see
        :meth:`FastChannelsConfig.for_region`)."""
        return any(GEO_PLACEHOLDER in model for model in (self.model, *self.allowed_models))


class ObservabilityConfig(_Section):
    transaction_search: TransactionSearchMode = TransactionSearchMode.OFF
    """``enable``: ``make account-baseline`` deploys the account-level FastChannels-AccountBaseline stack (an
    account-wide change: X-Ray spans are then billed as CloudWatch Logs), once per account and region before
    ``make deploy``; ``existing`` if it is already on in this account and region. Both turn on the agent's traces.
    ``make deploy`` and ``make destroy`` never touch the baseline stack."""
    log_retention_days: int = 7
    """Retention of every log group, and expiry of the logs bucket (S3 access and CloudFront logs)."""
    mediatailor_log_percent: int = Field(default=100, ge=0, le=100)
    """Share of MediaTailor sessions that emit vended logs (100 for demos, ~10 in production)."""
    as_run: AsRunMode = AsRunMode.PER_STAGE
    """``per_stage``: the stage subscribes to the as-run log itself (CloudWatch Logs allows five subscriptions per log
    group, so at most five such stages per account and region); ``shared``: the stage reads as-run events from the
    account's ``FastChannels-AsRun`` router through EventBridge, which ``make as-run-router`` deploys once per account
    and region (ADR-0032)."""
    capture_prompts: bool | None = None
    """Keep message content (operator prompts, the system prompt, tool results and model replies) in the agent
    runtime's OpenTelemetry logs and spans, where anyone who can read them sees every operator's conversation.
    Default: only when ``transaction_search`` is not ``off``, because the online evaluations score that content."""

    @property
    def records_prompts(self) -> bool:
        """``capture_prompts``, with its default applied."""
        if self.capture_prompts is not None:
            return self.capture_prompts
        return self.transaction_search is not TransactionSearchMode.OFF

    @field_validator("log_retention_days")
    @classmethod
    def _cloudwatch_retention(cls, days: int) -> int:
        if days not in _RETENTION:
            raise ValueError(f"CloudWatch Logs supports retention of {sorted(_RETENTION)} days, got {days}")
        return days

    @property
    def log_retention(self) -> logs.RetentionDays:
        """``log_retention_days`` as a CDK retention value."""
        return _RETENTION[self.log_retention_days]


class NotificationsConfig(_Section):
    email: str | None = Field(default=None, pattern=_EMAIL)
    """Email subscribed to alarms and asset/ladder failure events."""


class LedgerLock(StrEnum):
    """S3 Object Lock mode of the ledger archive."""

    OFF = "off"
    GOVERNANCE = "governance"
    COMPLIANCE = "compliance"


class _Module(_Section):
    """An optional module: nothing of it is deployed unless ``enabled``."""

    enabled: bool = False


class OpsModule(_Module):
    """The ops module (``modules.ops``)."""

    enabled: bool = False
    """Deploy the ops module: a per-stage data lake (S3, Glue, Athena), a hash-chained ledger of changes and money
    events, account quota alarms and detectors, runbooks and a probe canary."""
    lake_retention_days: int = Field(default=30, ge=1)
    """Days after which lake objects expire."""
    ledger_lock: LedgerLock = LedgerLock.GOVERNANCE
    """Object Lock mode of the ledger archive. ``compliance`` locks records even against the account root until
    their retention ends, so it needs ``removal: retain``; use it only in production, after a retention review."""
    ledger_retention_days: int = Field(default=30, ge=1, le=36_500)
    """Object Lock retention of each ledger record; until it ends, ``make destroy`` cannot delete the archive (1 on
    development stages)."""
    canary: bool = True
    """Run a synthetic playback canary on the probe playback configuration of each running channel."""


class ModelTask(TaskSettings):
    """One task's model-registry entry, rendered to SSM ``/<prefix>/models/tasks/<task>`` with ``{geo}`` resolved and
    the task's application inference profile."""

    @field_validator("model", mode="after")
    @classmethod
    def _resolve_model(cls, model: str, info: ValidationInfo) -> str:
        region = (info.context or {}).get("region")
        return resolve_geo(model, region) if region else model


class ModelsModule(_Module):
    """The model-registry module (``modules.models``)."""

    enabled: bool = False
    """Deploy the per-task model registry: one validated entry per LLM task, each with its own IAM role, application
    inference profile and spend cap. Entries choose only among ``agent.allowed_models``."""
    tasks: dict[str, ModelTask] = Field(
        # Validated like a value from cdk.json, so that {geo} resolves for the region.
        default_factory=lambda: {
            "scheduler.plan": {"model": DEFAULT_MODEL, "daily_cap_usd": SCHEDULER_DAILY_CAP_USD, "eval": SCHEDULER_EVAL}
        },
        validate_default=True,
    )
    """Registry entries by task name (dot-separated lowercase words); ``scheduler.plan`` is the scheduler agent."""
    budgets_action: bool = False
    """Add an AWS Budget per task role with a ``daily_cap_usd`` whose action, once the task's spend in the month
    reaches 31 days of the cap, denies the role's invocations of the task's inference profile (Budgets actions accept
    no daily budget). Needs ``notifications.email`` (AWS Budgets emails it when the action runs) and the ``fast:task``
    cost allocation tag activated in the payer account."""

    @field_validator("tasks")
    @classmethod
    def _task_names(cls, tasks: dict[str, ModelTask]) -> dict[str, ModelTask]:
        if bad := [name for name in tasks if not TASK_NAME.fullmatch(name)]:
            raise ValueError(f"task names must be dot-separated lowercase words, got {bad}")
        return tasks


class DemandMode(StrEnum):
    """Where bids come from: the own book alone, or the own book plus the bidder simulator."""

    OWN_BOOK = "own_book"
    DEMO = "demo"


class PbsVersion(StrEnum):
    """Prebid Server (Java) release the demand image is built from."""

    V3_43_0 = "3.43.0"
    V4_4_0 = "4.4.0"


class Simulator(StrEnum):
    """How the bidder simulator is deployed in demo mode."""

    GUIDANCE_STACK = "guidance_stack"
    LAMBDA = "lambda"


class DemandTasks(_Section):
    """Prebid Server task counts (ECS service capacity)."""

    min: int = Field(default=2, ge=1)
    """Prebid Server tasks while any channel runs (the tier minimum); 0 while none does."""
    max: int = Field(default=4, ge=1)
    """Most Prebid Server tasks (the ECS service maximum)."""


class DemandDaypart(_Section):
    """A scheduled raise of the Prebid Server task minimum ahead of a busy daypart."""

    cron: str = Field(pattern=r"^cron\(\S+( \S+){5}\)$")
    """Application Auto Scaling ``cron(minutes hours day-of-month month day-of-week year)`` expression (UTC); fire it
    at least 10 minutes before the daypart's first break."""
    min: int = Field(ge=1)
    """Task minimum from then on (between ``tasks.min`` and ``tasks.max``)."""


class DemandModule(_Module):
    """The demand module (``modules.demand``)."""

    enabled: bool = False
    """Deploy programmatic demand: the Guidance for Deploying a Prebid Server on AWS as its own app at a pinned tag,
    with the repository's overlays and Prebid Server module, the guidance's bidder simulator in demo mode,
    census-gated scaling and an origin rule that admits only this stage's MediaTailor requests."""
    mode: DemandMode = DemandMode.DEMO
    """``own_book``: own-book bids only (no simulator); ``demo``: the own book plus the guidance's bidder simulator,
    every number labelled simulated."""
    guidance_tag: str = Field(default="v1.4.0", pattern=r"^v\d+\.\d+\.\d+$")
    """Tag of the guidance, pinned with its commit and the hash of every file the overlays patch."""
    pbs_version: PbsVersion = PbsVersion.V3_43_0
    """Prebid Server (Java) release of the image."""
    simulator: Simulator = Simulator.GUIDANCE_STACK
    """``guidance_stack``: the guidance's simulator stack (its own VPC with two NAT gateways); ``lambda``: the same
    handler as a Lambda target behind an internal load balancer in the Prebid Server VPC, which needs no VPC of its
    own."""
    tasks: DemandTasks = DemandTasks()
    server_instances: int = Field(default=2, ge=1)
    """Prebid Server ``server.http.server-instances``: one event loop per task vCPU."""
    dayparts: tuple[DemandDaypart, ...] = ()
    """Scheduled task minimums ahead of busy dayparts; target tracking covers the time between them."""
    rate_limit_per_ip: int = Field(default=20_000, ge=10, le=2_000_000_000)
    """Requests per client IP per 60 s above which the origin's web ACL blocks (an abuse cap, far above any
    MediaTailor egress address's rate)."""

    @model_validator(mode="after")
    def _within_task_limits(self) -> Self:
        if self.tasks.min > self.tasks.max:
            raise ValueError(f"tasks.min ({self.tasks.min}) exceeds tasks.max ({self.tasks.max})")
        if outside := [d.cron for d in self.dayparts if not self.tasks.min <= d.min <= self.tasks.max]:
            raise ValueError(f"dayparts {outside}: min must lie between tasks.min and tasks.max")
        return self


class LineCaps(_Section):
    """Frequency caps of a demo line."""

    per_session: int | None = Field(default=None, ge=1)
    """Most impressions of the line per playback session; ``null``: uncapped."""


class DemoLine(_Section):
    """A synthetic direct line, bid by the own book like a sold insertion order (demo mode)."""

    id: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
    """Line id, unique among ``demo_lines``."""
    advertiser: str = Field(min_length=1, max_length=100)
    """Advertiser name (decision records and reports)."""
    adomain: str = Field(pattern=_DOMAIN)
    """Advertiser domain (OpenRTB ``adomain``); pods never repeat one."""
    cat: tuple[str, ...] = ()
    """IAB categories of the line's ads (OpenRTB ``cat``)."""
    creatives: tuple[str, ...] = Field(min_length=1)
    """Creative ids (``crid``) of the direct demo creatives the line serves (``direct1``, ``direct2``); a creative
    serves at most one line."""
    cpm: Decimal = Field(gt=0)
    """Price per 1,000 impressions, US dollars (simulated)."""
    budget: Decimal = Field(gt=0)
    """Total budget, US dollars (simulated); the line stops once reservations reach it."""
    caps: LineCaps = LineCaps()
    tier0: bool = False
    """A guaranteed line: placed first in a pod, ahead of any bid."""


class PdsConcurrency(_Section):
    """Provisioned concurrency of each hot-path function while any channel runs; 0 releases it.

    A channel's 100-session break herd needed at most 8 ``/policy``, 17 ``/select`` and 9 ``/ownbook/openrtb``
    environments at once with no spillover (fastcid, 2026-10-30); below that, the spilled requests waited for 2 s cold
    starts, which cascade. The defaults leave room above those peaks.
    """

    policy: int = Field(default=20, ge=0)
    select: int = Field(default=30, ge=0)
    ownbook: int = Field(default=20, ge=0)


class PdsModule(_Module):
    """The Pod Decision Service module (``modules.pds``)."""

    enabled: bool = False
    """Deploy the Pod Decision Service: the per-break policy, pod selection and own book (house ads and direct
    lines) that the Prebid Server module calls, with reservations, delivery states and a decision record per
    break."""
    policy_timeout_ms: int = Field(default=80, gt=0)
    """Timeout of the module's ``/policy`` call; on expiry the break is served from the own book alone. The handler
    takes under 3 ms, but its internal load balancer adds 20-40 ms to invoke a Lambda target (measured, M2-x2)."""
    policy_group_timeout_ms: int = Field(default=110, gt=0)
    """Timeout of the hook group that holds the ``/policy`` call (at least ``policy_timeout_ms``)."""
    select_timeout_ms: int = Field(default=200, gt=0)
    """Timeout of the module's ``/select`` call; on expiry the pod is built from house bids. ``/select`` reserves the
    pod's line ads in DynamoDB first: 25-50 ms, and up to about 110 ms on a first call after an idle spell of a minute
    or more (DynamoDB has dropped the connection), plus 20-40 ms of the load balancer's (measured, M2-x2)."""
    select_group_timeout_ms: int = Field(default=250, gt=0)
    """Timeout of the hook group that holds the ``/select`` call (at least ``select_timeout_ms``)."""
    provisioned_concurrency: PdsConcurrency = PdsConcurrency()
    """Provisioned concurrency per hot-path function while any channel runs (none while none does)."""
    demo_lines: tuple[DemoLine, ...] = Field(default=(), max_length=8)
    """Synthetic direct lines served by the own book in demo mode, at most 8 (they travel in the snapshot publisher's
    environment). Each line's budget is split over the own book's budget shards; a line stops serving once
    reservations hold all of it."""

    @model_validator(mode="after")
    def _groups_hold_their_calls(self) -> Self:
        problems = [
            f"{call}_group_timeout_ms ({group}) is shorter than {call}_timeout_ms ({timeout})"
            for call, timeout, group in (
                ("policy", self.policy_timeout_ms, self.policy_group_timeout_ms),
                ("select", self.select_timeout_ms, self.select_group_timeout_ms),
            )
            if group < timeout
        ]
        if problems:
            raise ValueError("\n".join(problems))
        _unique(tuple(line.id for line in self.demo_lines))
        served = tuple(crid for line in self.demo_lines for crid in line.creatives)
        try:
            _unique(served)
        except ValueError as error:
            raise ValueError(f"demo_lines: a creative serves at most one line: {error}") from None
        if unknown := sorted(set(served) - set(crids(DemoRole.DIRECT))):
            raise ValueError(f"demo_lines: not direct demo creatives: {', '.join(unknown)}")
        return self


class ReleaseStrategy(StrEnum):
    """AppConfig deployment strategy of the release pointer, by audience size."""

    DEMO = "demo"
    STANDARD = "standard"
    LARGE = "large"


class ReleasePrefetch(_Section):
    """Ad prefetch on the monetized siblings (``modules.release.prefetch``; DESIGN §10)."""

    enabled: bool = True
    """Create a SINGLE prefetch schedule for each of the next breaks of a running monetized channel, so MediaTailor
    asks the ad decision server minutes ahead, spread over a window, and transcodes new creatives before they air.
    Retrieval transcodes bill whether or not the break airs; the horizon stays short and re-planned breaks lose their
    schedules."""
    siblings: tuple[str, ...] = ("a", "b", "k", "l")
    """Sibling letters that get schedules: the general pair and the kids pair (the probe ``z`` never does)."""
    horizon_breaks: int = Field(default=20, ge=1, le=24)
    """Upcoming breaks with a schedule, at most (MediaTailor allows 25 active schedules per configuration)."""
    retrieval_lead_seconds: int = Field(default=150, ge=60, le=3600)
    """Retrieval opens this long before the break."""
    retrieval_end_seconds: int = Field(default=30, ge=10)
    """Retrieval closes this long before the break (transcodes take about 20 s)."""
    shaping_window_seconds: int = Field(default=90, ge=0)
    """MediaTailor spreads the sessions' prefetch requests over this many seconds (``RETRIEVAL_WINDOW`` traffic
    shaping); 0 sends them as the sessions arrive."""

    @field_validator("siblings")
    @classmethod
    def _letters(cls, letters: tuple[str, ...]) -> tuple[str, ...]:
        if invalid := [s for s in letters if s not in {"a", "b", "k", "l"}]:
            raise ValueError(f"sibling letters are a, b (general) and k, l (kids); got {invalid}")
        return _unique(letters)

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.retrieval_end_seconds >= self.retrieval_lead_seconds:
            raise ValueError("retrieval_end_seconds must be less than retrieval_lead_seconds")
        return self


class ReleaseModule(_Module):
    """The release module (``modules.release``)."""

    enabled: bool = False
    """Deploy the Release Controller: each monetized channel gets a pair of playback configurations with MediaTailor
    Functions; a release goes to the idle one through a smoke test, is promoted by a gradual AppConfig pointer and
    is rolled back unattended on an alarm."""
    channels: tuple[str, ...] = ()
    """IaC channels to monetize (names from ``channels.iac``; ``{prefix}`` is replaced as there). Their channel policy
    also admits the sibling configurations ``<channel>-p-?``. Adding a channel widens its policy once, and the
    deployment refuses that while the channel runs: stop it first."""
    strategy: ReleaseStrategy = ReleaseStrategy.DEMO
    """``demo`` (a simulated audience: 10% steps over 30 minutes), ``standard`` (up to 20,000 concurrent viewers: 10%
    steps over 3 hours) or ``large`` (exponential steps over 4 hours)."""
    lab_channel: bool = True
    """Create the stopped lab channel ``<prefix>-lab``, started only to smoke-test a release while the monetized
    channel is stopped."""
    probe_sibling: bool = True
    """Create a house-only probe playback configuration per channel, for canaries and smoke tests."""
    secret_rotation_days: int = Field(default=90, ge=1)
    """Days between rotations of the origin secret that MediaTailor sends to the demand origin."""
    prefetch: ReleasePrefetch = ReleasePrefetch()

    @field_validator("channels")
    @classmethod
    def _unique_channels(cls, channels: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(channels)


class CrawlModule(_Module):
    """The content-metadata module (``modules.crawl``)."""

    enabled: bool = False
    """Deploy show-level content metadata: a catalog context object per title, a cached PRE_ADS lookup by UPID and
    the OpenRTB ``app.content`` fields in each ad request, with a holdout that measures the uplift."""
    pre_ads: bool = True
    """Map the cached PRE_ADS lookup on the monetized playback configurations."""
    holdout_percent: int = Field(default=20, ge=0, le=100)
    """Share of session-breaks that get no enrichment, the uplift measurement's control arm."""
    publisher_domain: str | None = Field(default=None, pattern=_DOMAIN)
    """Your domain (e.g. ``example.com``), named as the registry of your catalog's content ids in the catalog context
    objects and in OpenRTB ``content.data[].cids``; unset, the content id travels in ``content.id`` only."""


_MODULE_NEEDS: Final = {
    "demand": ("ops",),
    "pds": ("ops", "demand"),
    "release": ("ops", "demand", "pds"),
    "crawl": ("pds", "release"),
}
"""Modules an enabled module builds on."""


def _listed(names: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)


class ModulesConfig(_Section):
    """Optional modules, each off by default; with every module off the deployment is exactly v3.0.0. Until a
    module's code is merged, enabling it changes nothing."""

    ops: OpsModule = OpsModule()
    models: ModelsModule = ModelsModule()
    demand: DemandModule = DemandModule()
    pds: PdsModule = PdsModule()
    release: ReleaseModule = ReleaseModule()
    crawl: CrawlModule = CrawlModule()

    def enabled(self, name: str) -> bool:
        """Whether module ``name`` (a field of this section) is on."""
        module: _Module = getattr(self, name)
        return module.enabled

    @model_validator(mode="after")
    def _needs_met(self) -> Self:
        problems = [
            f"{name} needs {_listed(missing)}: enable {'them' if len(missing) > 1 else 'it'} or turn {name} off"
            for name, needs in _MODULE_NEEDS.items()
            if self.enabled(name) and (missing := [need for need in needs if not self.enabled(need)])
        ]
        if problems:
            raise ValueError("\n".join(problems))
        return self


class FastChannelsConfig(_Section):
    """FAST Channels deployment configuration (cdk.json context ``fast-channels``)."""

    prefix: str = Field(default="fast", pattern=PREFIX_PATTERN)
    """Name prefix of every resource and value of the ``fast:app`` tag: 2-16 lowercase letters and digits, starting
    with a letter, without hyphens. Change it to deploy several copies in one account and region. Names and IAM grants
    are scoped by ``<prefix>-*``, so a prefix with a hyphen (``fast-x``) would fall inside another's (``fast-*``)."""
    removal: Removal = Removal.DESTROY
    """``destroy`` (demo: delete data with the stacks) or ``retain`` (production: keep data, enable deletion and
    termination protection)."""
    ladders: LaddersConfig = LaddersConfig()
    channel_assembly: ChannelAssemblyConfig = ChannelAssemblyConfig()
    channels: ChannelsConfig = ChannelsConfig()
    delivery: DeliveryConfig = DeliveryConfig()
    ads: AdsConfig = PreconditionedAds()
    agent: AgentConfig = AgentConfig()
    observability: ObservabilityConfig = ObservabilityConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    modules: ModulesConfig = ModulesConfig()

    @field_validator("prefix", mode="before")
    @classmethod
    def _isolated_prefix(cls, prefix: object) -> object:
        """Says why a prefix is refused (the pattern alone would not)."""
        return check_prefix(prefix) if isinstance(prefix, str) else prefix

    @property
    def removal_policy(self) -> RemovalPolicy:
        """CDK removal policy of stateful resources."""
        return RemovalPolicy.RETAIN if self.removal is Removal.RETAIN else RemovalPolicy.DESTROY

    def ladder(self, name: str) -> Ladder:
        """Built-in ladder ``name`` (``ladders/<name>.toml``), loaded once."""
        return _ladder(name)

    @model_validator(mode="after")
    def _consistent(self, info: ValidationInfo) -> Self:
        region: str | None = (info.context or {}).get("region")
        problems = [problem for channel in self.channels.iac for problem in self._channel_problems(channel)]
        problems += self._module_problems(region)
        if region:
            problems += self._region_problems(region)
        if problems:
            raise ValueError("\n".join(problems))
        return self

    @property
    def lab_channel(self) -> ChannelConfig | None:
        """The release module's lab channel ``<prefix>-lab`` (``modules.release.lab_channel``): BASIC, created
        stopped, with the first monetized channel's ladder and outputs. It is started only to smoke-test a release
        while the monetized channels are stopped, and has siblings like them."""
        release = self.modules.release
        if not (release.enabled and release.lab_channel and release.channels):
            return None
        first = next(c for c in self.channels.iac if c.name == release.channels[0])
        return ChannelConfig(name=f"{self.prefix}-lab", ladder=first.ladder, outputs=first.outputs)

    @property
    def iac_channels(self) -> tuple[ChannelConfig, ...]:
        """``channels.iac`` and the lab channel."""
        return (*self.channels.iac, *((self.lab_channel,) if self.lab_channel else ()))

    @property
    def release_channels(self) -> tuple[str, ...]:
        """The channels with siblings: ``modules.release.channels`` and the lab channel (none with the module
        off)."""
        if not self.modules.release.enabled:
            return ()
        return (*self.modules.release.channels, *((self.lab_channel.name,) if self.lab_channel else ()))

    def _channel_problems(self, channel: ChannelConfig) -> list[str]:
        where = f"{CONTEXT_KEY}.channels.iac[{channel.name}]"
        if not channel.name.startswith(f"{self.prefix}-") or is_agent_owned(channel.name, self.prefix):
            return [f"{where}: name must start with {self.prefix!r}- but not {self.prefix!r}-agent- (agent-owned)"]
        if problem := self._sibling_problem(channel.name):
            return [f"{where}: {problem}"]
        if channel.ladder not in self.ladders.builtin:
            return [f"{where}: ladder {channel.ladder!r} is not in ladders.builtin {list(self.ladders.builtin)}"]
        if extra := [o for o in channel.outputs if o not in self.channel_assembly.package_profiles]:
            return [f"{where}: outputs {extra} are not in channel_assembly.package_profiles"]
        return []

    def _sibling_problem(self, name: str) -> str | None:
        """Why channel ``name`` would collide with the release module's siblings (``<channel>-p-<letter>``, granted as
        ``<channel>-p-*``), whether or not the module is on: so that turning it on cannot."""
        siblings = sibling_pattern(name)
        if siblings.startswith(agent_namespace(self.prefix)):
            return (
                f"{name!r} cannot be an IaC channel: the release module would name its siblings {siblings!r}, inside "
                f"the agent-owned {agent_namespace(self.prefix)}* names"
            )
        lab = f"{self.prefix}-lab"
        channels = {*(c.name for c in self.channels.iac), lab} - {name}
        if parent := next((c for c in sorted(channels) if fnmatchcase(name, sibling_pattern(c))), None):
            of = f"the release lab channel {parent!r}" if parent == lab else f"channel {parent!r}"
            return (
                f"{name!r} is a sibling name of {of}: the release module names that channel's playback configurations "
                f"{parent}-p-<letter> and its IAM grants cover {sibling_pattern(parent)!r}; rename the channel"
            )
        return None

    def _module_problems(self, region: str | None) -> list[str]:
        """Rules between a module and the rest of the configuration (rules among modules: :class:`ModulesConfig`).
        Model ids are compared with ``{geo}`` resolved for ``region``: a section left at its defaults keeps it."""
        modules, where = self.modules, f"{CONTEXT_KEY}.modules"
        iac = [channel.name for channel in self.channels.iac]
        problems = [
            f"{where}.release.channels: {name!r} is not an IaC channel (channels.iac: {iac})"
            for name in modules.release.channels
            if name not in iac
        ]
        if modules.release.enabled and modules.release.lab_channel and (lab := f"{self.prefix}-lab") in iac:
            problems.append(
                f"{where}.release.lab_channel: {lab!r} is the release lab channel; rename the IaC channel or set "
                "lab_channel=false"
            )
        if modules.demand.enabled and len(self.prefix) > QUALIFIER_MAX:
            problems.append(
                f"{where}.demand: the Prebid Server guidance deploys onto a CDK bootstrap qualified by the prefix, "
                f"at most {QUALIFIER_MAX} characters; {self.prefix!r} is {len(self.prefix)}"
            )
        if modules.ops.enabled and modules.ops.ledger_lock is LedgerLock.COMPLIANCE and self.removal is Removal.DESTROY:
            problems.append(
                f"{where}.ops.ledger_lock: compliance-mode records cannot be deleted before their retention ends, "
                "which removal=destroy needs; use governance or removal=retain"
            )
        if modules.models.enabled:
            allowed = [resolve_geo(model, region) if region else model for model in self.agent.allowed_models]
            problems += [
                f"{where}.models.tasks[{task}]: model {model!r} is not in agent.allowed_models {allowed}"
                for task, entry in modules.models.tasks.items()
                if (model := resolve_geo(entry.model, region) if region else entry.model) not in allowed
            ]
            problems += [
                f"{where}.models.tasks[{task}]: {problem}"
                for task, entry in modules.models.tasks.items()
                for problem in qualification(eval_records(), task, entry)
            ]
            if modules.models.budgets_action and not self.notifications.email:
                problems.append(
                    f"{where}.models.budgets_action: AWS Budgets emails notifications.email when an action runs; "
                    "set it or turn budgets_action off"
                )
            if self.agent.enabled and SCHEDULER_TASK not in modules.models.tasks:
                problems.append(
                    f"{where}.models.tasks: {SCHEDULER_TASK!r}, the scheduler agent's entry, is missing; add it or set "
                    "agent.enabled=false"
                )
        return problems

    def _region_problems(self, region: str) -> list[str]:
        if region not in MEDIA_REGIONS:
            return [
                f"region {region}: MediaTailor Channel Assembly and MediaConvert are not both available there; "
                f"use one of {sorted(MEDIA_REGIONS)}"
            ]
        if self.agent.enabled and region not in AGENTCORE_REGIONS:
            return [
                f"region {region}: Amazon Bedrock AgentCore is not available; set agent.enabled=false or use "
                f"one of {sorted(AGENTCORE_REGIONS)}"
            ]
        if not (self.agent.enabled or self.modules.models.enabled):  # nothing invokes the allowed models
            return []
        local = next((geo for geo, prefixes in _GEO_PROFILE_REGIONS.items() if region.startswith(prefixes)), "global")
        return [
            f"{CONTEXT_KEY}.agent.allowed_models: {model!r} is a {geo}. inference profile, which {region} cannot "
            f"invoke; use {local}.{name} or global.{name}"
            for model in self.agent.allowed_models
            if (geo := model.partition(".")[0]) in _GEO_PROFILE_REGIONS
            and not region.startswith(_GEO_PROFILE_REGIONS[geo])
            for name in [model.partition(".")[2]]
        ]

    def for_region(self, region: str) -> Self:
        """This configuration validated for ``region``: ``{geo}`` resolved and the region's limits checked.

        Raises:
            ConfigError: listing every problem, as :meth:`from_context` does.
        """
        try:
            return self.model_validate(self.model_dump(), context={"region": region})
        except ValidationError as error:
            raise ConfigError(_describe(error)) from None

    @classmethod
    def from_context(cls, node: Node, *, region: str | None = None) -> Self:
        """Load the configuration from CDK context (see the module docstring), validating it for ``region``.

        Raises:
            ConfigError: listing every invalid field with its ``fast-channels.<path>``.
        """
        context = node.get_all_context()
        data = _decode(context.get(CONTEXT_KEY, {}))
        if not isinstance(data, dict):
            raise ConfigError(f"context {CONTEXT_KEY!r} must be a JSON object, got {data!r}")
        for key in sorted(k for k in context if k.startswith(f"{CONTEXT_KEY}.")):
            _merge_path(data, key.removeprefix(f"{CONTEXT_KEY}.").split("."), _decode(context[key]))
        _expand_prefix(data)
        try:
            return cls.model_validate(data, context={"region": region})
        except ValidationError as error:
            raise ConfigError(_describe(error)) from None


def eval_records() -> tuple[Path, ...]:
    """Where eval records are read from: :data:`EVAL_RECORDS_ENV` when set, else :data:`EVAL_RECORDS`."""
    configured = os.environ.get(EVAL_RECORDS_ENV)
    return tuple(Path(d) for d in configured.split(os.pathsep) if d) if configured else (EVAL_RECORDS,)


def _expand_prefix(data: dict[str, Any]) -> None:
    """Replace ``{prefix}`` in IaC channel names and in the channels the release module monetizes, so one default
    channel works for every deployment prefix."""
    prefix = data.get("prefix")
    if not isinstance(prefix, str):
        return
    channels = data.get("channels")
    for channel in (channels.get("iac") or ()) if isinstance(channels, dict) else ():
        if isinstance(channel, dict) and isinstance(channel.get("name"), str):
            channel["name"] = channel["name"].replace("{prefix}", prefix)
    modules = data.get("modules")
    release = modules.get("release") if isinstance(modules, dict) else None
    if isinstance(release, dict) and isinstance(release.get("channels"), list):
        release["channels"] = [
            name.replace("{prefix}", prefix) if isinstance(name, str) else name for name in release["channels"]
        ]


def _decode(value: Any) -> Any:
    """CLI context values arrive as strings: parse JSON where possible, otherwise keep the string."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _merge_path(tree: dict[str, Any] | list[Any], path: list[str], value: Any) -> None:
    head, *rest = path
    if isinstance(tree, list):
        if not head.isdigit() or int(head) >= len(tree):
            raise ConfigError(f"{CONTEXT_KEY} override: {head!r} is not an index of a {len(tree)}-item list")
        key: Any = int(head)
        current = tree[key]
    else:
        key, current = head, tree.get(head)
    if rest:
        if not isinstance(current, dict | list):
            current = {}
            tree[key] = current
        _merge_path(current, rest, value)
    elif isinstance(current, dict) and isinstance(value, dict):
        for sub_key, sub_value in value.items():
            _merge_path(current, [sub_key], sub_value)
    else:
        tree[key] = value


def _describe(error: ValidationError) -> str:
    """One line per problem; field errors carry their path, cross-field problems name theirs in the message."""
    lines = []
    for e in error.errors():
        path = ".".join([CONTEXT_KEY, *map(str, e["loc"])])
        message = re.sub(r"^Value error, ", "", e["msg"])
        lines += [f"  - {path}: {line}" if e["loc"] else f"  - {line}" for line in message.splitlines()]
    return "invalid FAST Channels configuration:\n" + "\n".join(lines)
