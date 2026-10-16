"""Deployment configuration: the ``fast-channels`` block of the CDK context, validated by pydantic.

The block lives in ``cdk.json``. Override any field at synth time with ``-c fast-channels.<path>=<value>``; values
are parsed as JSON when they can be (``-c fast-channels.agent.enabled=false``,
``-c fast-channels.channels.iac='[{"name": "fast-news"}]'``), list items are addressed by index
(``-c fast-channels.channels.iac.0.tier=STANDARD``) and JSON objects are deep-merged. ``-c fast-channels='<json>'``
replaces the whole block (CDK CLI context semantics); fields it omits take the defaults below.

Every attribute docstring below is the field's documentation (also exported in the JSON schema).
"""

import json
import re
from enum import StrEnum
from functools import cache
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Self

from aws_cdk import RemovalPolicy
from aws_cdk import aws_logs as logs
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from fast_channels.core.ladder import Ladder
from fast_channels.core.naming import is_agent_owned
from fast_channels.core.profiles import CATALOGUE, DEFAULT_PACKAGE_PROFILES, applicable, profile
from fast_channels.core.specs import validate_ads_url
from fast_channels.core.timeline import ProgrammingBrief  # noqa: TC001 - pydantic resolves it at runtime

if TYPE_CHECKING:
    from constructs import Node

CONTEXT_KEY: Final = "fast-channels"
GEO_PLACEHOLDER: Final = "{geo}"
"""Stands for the deployment region's inference-profile geography in model ids (:func:`geo_prefix`)."""
DEFAULT_MODEL: Final = f"{GEO_PLACEHOLDER}.anthropic.claude-haiku-5-5"
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
    the agent creates. ``{prefix}`` is replaced with the deployment prefix (the default is ``{prefix}-demo``)."""
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
    content_domain: str | None = Field(default=None, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9-]+)+$")
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
    """A third-party ad decision server; MediaTailor transcodes its creatives (single-codec outputs only)."""

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
    """``enable`` deploys the account-level FastChannels-AccountBaseline stack (an account-wide change: X-Ray spans
    are then billed as CloudWatch Logs); ``existing`` if it is already on in this account and region."""
    log_retention_days: int = 7
    """Retention of every log group, and expiry of the logs bucket (S3 access and CloudFront logs)."""
    mediatailor_log_percent: int = Field(default=100, ge=0, le=100)
    """Share of MediaTailor sessions that emit vended logs (100 for demos, ~10 in production)."""
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


class FastChannelsConfig(_Section):
    """FAST Channels deployment configuration (cdk.json context ``fast-channels``)."""

    prefix: str = Field(default="fast", pattern=r"^[a-z][a-z0-9-]{0,14}[a-z0-9]$")
    """Name prefix of every resource and value of the ``fast:app`` tag. Change it to deploy several copies in one
    account and region."""
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

    @property
    def removal_policy(self) -> RemovalPolicy:
        """CDK removal policy of stateful resources."""
        return RemovalPolicy.RETAIN if self.removal is Removal.RETAIN else RemovalPolicy.DESTROY

    @model_validator(mode="after")
    def _consistent(self, info: ValidationInfo) -> Self:
        problems = [
            *self._ladder_problems(),
            *(problem for channel in self.channels.iac for problem in self._channel_problems(channel)),
        ]
        if region := (info.context or {}).get("region"):
            problems += self._region_problems(region)
        if problems:
            raise ValueError("\n".join(problems))
        return self

    def _ladder_problems(self) -> list[str]:
        return [
            f"{CONTEXT_KEY}.channel_assembly.package_profiles: {name!r} selects no rendition of ladder {ladder!r}"
            for ladder in self.ladders.builtin
            for name in self.channel_assembly.package_profiles
            if profile(name) not in applicable(_ladder(ladder))
        ]

    def _channel_problems(self, channel: ChannelConfig) -> list[str]:
        where = f"{CONTEXT_KEY}.channels.iac[{channel.name}]"
        if not channel.name.startswith(f"{self.prefix}-") or is_agent_owned(channel.name, self.prefix):
            return [f"{where}: name must start with {self.prefix!r}- but not {self.prefix!r}-agent- (agent-owned)"]
        if channel.ladder not in self.ladders.builtin:
            return [f"{where}: ladder {channel.ladder!r} is not in ladders.builtin {list(self.ladders.builtin)}"]
        if extra := [o for o in channel.outputs if o not in self.channel_assembly.package_profiles]:
            return [f"{where}: outputs {extra} are not in channel_assembly.package_profiles"]
        ladder = _ladder(channel.ladder)
        if empty := [o for o in channel.outputs if profile(o) not in applicable(ladder)]:
            return [f"{where}: outputs {empty} select no rendition of ladder {channel.ladder!r}"]
        if self.ads.mode is AdsMode.EXTERNAL_VAST and (mixed := _mixed_codec_outputs(ladder, channel.outputs)):
            return [
                f"{where}: outputs {mixed} mix codecs, but external_vast creatives are transcoded by "
                "MediaTailor, which only works on single-codec outputs such as hls-avc"
            ]
        return []

    def _region_problems(self, region: str) -> list[str]:
        if region not in MEDIA_REGIONS:
            return [
                f"region {region}: MediaTailor Channel Assembly and MediaConvert are not both available there; "
                f"use one of {sorted(MEDIA_REGIONS)}"
            ]
        if not self.agent.enabled:
            return []
        if region not in AGENTCORE_REGIONS:
            return [
                f"region {region}: Amazon Bedrock AgentCore is not available; set agent.enabled=false or use "
                f"one of {sorted(AGENTCORE_REGIONS)}"
            ]
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


def _mixed_codec_outputs(ladder: Ladder, outputs: tuple[str, ...]) -> list[str]:
    return [o for o in outputs if len({r.codec for r in ladder.video if profile(o).selects(r)}) > 1]


def _expand_prefix(data: dict[str, Any]) -> None:
    """Replace ``{prefix}`` in IaC channel names, so one default channel works for every deployment prefix."""
    prefix = data.get("prefix")
    channels = data.get("channels")
    if not isinstance(prefix, str) or not isinstance(channels, dict):
        return
    for channel in channels.get("iac") or ():
        if isinstance(channel, dict) and isinstance(channel.get("name"), str):
            channel["name"] = channel["name"].replace("{prefix}", prefix)


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
