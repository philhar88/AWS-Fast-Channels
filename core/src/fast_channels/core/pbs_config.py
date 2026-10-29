"""Prebid Server configuration of the demand module: the files the AWS Prebid Server guidance loads (docs/demand.md).

Pure renderers, without I/O, for one stage (:func:`render`):

- ``current-config/prebid-config.yaml``: the guidance copies ``prebid-server/current/`` over ``default/`` file for
  file, so this file replaces the guidance's default ``prebid-config.yaml`` and repeats every default it keeps. It
  turns the guidance's five shipped adapters off, the bidder simulator (``amt``) on in demo mode, adds the own book as
  a ``generic`` alias on the Pod Decision Service and configures the fast-ctv module.
- ``stored_requests/fast-pod-<n>s.json`` and ``fast-pod-kids-<n>s.json``: one structured pod of 30 s slots per break
  length on the 30.03 s grid. They name only the own book: third-party bidders, IDs and user data are added by the
  fast-ctv policy hook after a positive, in-time ``/policy`` answer, so a hook that is skipped, slow, failing or
  absent leaves own book and house only (the no-hook contract).
- ``stored_accounts/<prefix>.json``: the stage's account, with the module's execution plan and the Activity Controls.

MediaTailor asks for ``fast-pod-{{session.avail_duration_secs}}s``. The seconds round half up, and a missing duration
becomes 300 s, so an off-grid or unknown break names a stored request that does not exist: Prebid Server answers
400 and the Channel Assembly slate plays.

Nothing rendered here is secret: the origin secret lives in the WAF rule group, never in Prebid Server.
"""

import json
import re
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

POD_SECONDS: Final = (30, 60, 90, 120)
"""Break lengths with a stored request: one to four 30 s slots (the 30.03 s grid, rounded)."""
SLOT_SECONDS: Final = 30
TMAX_MS: Final = 500
OWN_BOOK: Final = "ownbook"
"""Bidder code of the own book: a ``generic`` alias on the Pod Decision Service's ``/ownbook/openrtb``."""
OWN_BOOK_MAXBIDS: Final = 9
"""Bids per slot that Prebid Server keeps from the own book (``ext.prebid.multibid``). Without it, Prebid Server keeps
one bid per bidder and slot, the best, and the house ads would never reach the module. Per slot ``/select`` needs the
own book's winner, a standalone for MediaTailor's ad buffet and the house fillers; the own book bids each house ad of
the kit (2) and each line's creative that fits (one per line: 2 in demo mode), 4 today. 9, Prebid Server's most,
keeps every bid of up to 7 lines; beyond that the lowest-priced bids, the house ads first, are cut."""
SIMULATOR: Final = "amt"
"""Bidder code of the guidance's bidder simulator (its ``amt`` adapter)."""
SHIPPED_ADAPTERS: Final = ("appnexus", "ix", "openx", "pubmatic", "rubicon")
"""Adapters the guidance's default configuration enables; turned off until a seat is qualified."""
MODULE: Final = "fast-ctv"
POLICY_HOOK: Final = "fast-ctv-policy"
VAST_HOOK: Final = "fast-ctv-vast"
AUCTION_ENDPOINT: Final = "/openrtb2/auction"
TRACKER_PATH: Final = "/ads/b.gif"
TRACKER_QUERY: Final = "d={sid}.{evid}&c={crid}&e={event}&enr={enr}"
"""Our impression tracker's query; the module fills the placeholders, URL-encoded, for every rendered ad."""
CONTENT_CATTAX: Final = 9
"""IAB Content Taxonomy 3.1 (AdCOM ``cattax`` 9)."""
VIDEO_MIMES: Final = ("application/x-mpegURL", "video/mp4")
VIDEO_PROTOCOLS: Final = (3, 6, 7, 8)
"""VAST 3.0 and 4.0, inline and wrapper (AdCOM List: Creative Subtypes - Audio/Video)."""
_HOST: Final = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")


SIMULATOR_RECORD: Final = "sim"
"""The bidder simulator's record in the stage's private zone (``lambda`` mode)."""


def private_zone(prefix: str) -> str:
    """The stage's private hosted zone in the Prebid Server VPC: ``<prefix>.fast.internal``."""
    return f"{prefix}.fast.internal"


def pds_host(prefix: str) -> str:
    """Stable name of the Pod Decision Service's internal load balancer, known before the service exists."""
    return f"pds.{private_zone(prefix)}"


def simulator_host(prefix: str) -> str:
    """Stable name of the bidder simulator in ``lambda`` mode (an internal load balancer in the Prebid Server VPC)."""
    return f"{SIMULATOR_RECORD}.{private_zone(prefix)}"


def stored_request_id(seconds: int, *, kids: bool = False) -> str:
    """``fast-pod-<seconds>s`` (``fast-pod-kids-<seconds>s`` for child-directed playback configurations)."""
    return f"fast-pod-{'kids-' if kids else ''}{seconds}s"


class PbsSettings(BaseModel):
    """What one stage's Prebid Server configuration depends on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prefix: str = Field(pattern=r"^[a-z][a-z0-9-]{0,14}[a-z0-9]$")
    """Deployment prefix: the account id, the publisher id and the private zone's name."""
    simulator: bool
    """Demo mode: the guidance's bidder simulator (``amt``) may bid when ``/policy`` adds it."""
    server_instances: int = Field(ge=1)
    """``server.http.server-instances``: one Vert.x event loop per task vCPU."""
    tracker_base_url: str = Field(pattern=r"^https://[a-z0-9.-]+$")
    """Scheme and host of the stage's CDN, which serves our impression tracker ``/ads/b.gif``."""
    publisher_domain: str
    """Domain in ``source.schain`` (the seller's advertising system)."""
    app_name: str = Field(default="FAST Channels", min_length=1, max_length=100)
    policy_timeout_ms: int = Field(default=50, gt=0)
    policy_group_timeout_ms: int = Field(default=80, gt=0)
    select_timeout_ms: int = Field(default=100, gt=0)
    select_group_timeout_ms: int = Field(default=150, gt=0)

    @field_validator("publisher_domain")
    @classmethod
    def _domain(cls, domain: str) -> str:
        if not _HOST.fullmatch(domain):
            raise ValueError(f"not a domain name: {domain!r}")
        return domain

    @property
    def pds_base_url(self) -> str:
        return f"http://{pds_host(self.prefix)}"

    @property
    def tracker_template(self) -> str:
        return f"{self.tracker_base_url}{TRACKER_PATH}?{TRACKER_QUERY}"


def tracker_host(cdn_base_url: str) -> str:
    """The host of a CDN base URL (``https://d1.cloudfront.net/`` -> ``d1.cloudfront.net``)."""
    host = urlsplit(cdn_base_url).hostname
    if not host:
        raise ValueError(f"not a URL with a host: {cdn_base_url!r}")
    return host


def _slot(index: int, count: int, poddur: int) -> dict[str, Any]:
    """Slot ``index`` (1-based) of ``count``: ``slotinpod`` 1 first, -1 last, 0 between (and for a single slot)."""
    position = 1 if index == 1 else -1 if index == count else 0
    return {
        "id": f"s{index}",
        "video": {
            "mimes": list(VIDEO_MIMES),
            "protocols": list(VIDEO_PROTOCOLS),
            "w": 1920,
            "h": 1080,
            "plcmt": 1,
            "linearity": 1,
            "startdelay": -1,
            "podid": 1,
            "poddur": poddur,
            "rqddurs": [SLOT_SECONDS],
            "slotinpod": position if count > 1 else 0,
        },
        "ssai": 3,
        "ext": {"prebid": {"bidder": {OWN_BOOK: {}}}},
    }


def stored_request(settings: PbsSettings, seconds: int, *, kids: bool = False) -> dict[str, Any]:
    """The stored request for a ``seconds`` break: ``seconds / 30`` structured slots of one pod, own book only, which
    may bid several ads per slot (:data:`OWN_BOOK_MAXBIDS`).

        Raises:
            ValueError: ``seconds`` is not one of :data:`POD_SECONDS`.
    """
    if seconds not in POD_SECONDS:
        raise ValueError(f"no stored request for {seconds} s breaks: the grid is {POD_SECONDS}")
    count = seconds // SLOT_SECONDS
    return {
        "id": stored_request_id(seconds, kids=kids),
        "at": 1,
        "tmax": TMAX_MS,
        "cur": ["USD"],
        "app": {
            "id": f"{settings.prefix}-app",
            "name": settings.app_name,
            "publisher": {"id": settings.prefix},
            "content": {"livestream": 1, "context": 1, "cattax": CONTENT_CATTAX},
        },
        "source": {
            "schain": {
                "complete": 1,
                "ver": "1.0",
                "nodes": [{"asi": settings.publisher_domain, "sid": settings.prefix, "hp": 1}],
            }
        },
        "regs": {"coppa": 1 if kids else 0},
        "imp": [_slot(index, count, seconds) for index in range(1, count + 1)],
        "ext": {"prebid": {"multibid": [{"bidder": OWN_BOOK, "maxbids": OWN_BOOK_MAXBIDS}]}},
    }


def _group(timeout_ms: int, hook: str) -> dict[str, Any]:
    return {"groups": [{"timeout": timeout_ms, "hook-sequence": [{"module-code": MODULE, "hook-impl-code": hook}]}]}


def _own_book_only() -> dict[str, Any]:
    """An activity that only the own book may perform."""
    # Prebid Server Java 3.43.0 reads the component type by its enum name: "bidder", as the Prebid documentation
    # spells it, makes the whole account unreadable (found by the contract tests).
    rule = {"condition": {"component_type": ["BIDDER"], "component_name": [OWN_BOOK]}, "allow": True}
    return {"default": False, "rules": [rule]}


def account(settings: PbsSettings) -> dict[str, Any]:
    """The stage's account: the module's execution plan, and Activity Controls that keep IDs, user first-party data
    and precise geolocation from every bidder but the own book (every Phase-1 session is contextual)."""
    return {
        "id": settings.prefix,
        "status": "active",
        "auction": {"debug-allow": False},
        "hooks": {
            "execution-plan": {
                "endpoints": {
                    AUCTION_ENDPOINT: {
                        "stages": {
                            "processed-auction-request": _group(settings.policy_group_timeout_ms, POLICY_HOOK),
                            "exitpoint": _group(settings.select_group_timeout_ms, VAST_HOOK),
                        }
                    }
                }
            }
        },
        "privacy": {
            "allowactivities": {
                activity: _own_book_only() for activity in ("transmitEids", "transmitUfpd", "transmitPreciseGeo")
            }
        },
    }


def prebid_config(settings: PbsSettings) -> dict[str, Any]:
    """The Prebid Server application configuration (``prebid-config.yaml``), as data.

    ``${NAME}`` values are Spring placeholders for environment variables the guidance's task definition and
    entrypoint set. Differences from the guidance's default file: ``server.http.server-instances``; the shipped
    adapters off, the simulator per mode and the own book; the module; GDPR out of scope by default (the stage serves
    the United States; a request that signals GDPR is still enforced); the unprotected log-level endpoint off.
    """
    pds = settings.pds_base_url
    return {
        "status-response": "ok",
        "server": {
            # The ALB reaches the tasks over TLS on 8443 (the guidance's required settings).
            "http": {"enabled": True, "port": 8443, "server-instances": settings.server_instances},
            "ssl": True,
            "jks-path": "${SSL_KEYSTORE_PATH}",
            "jks-password": "${SSL_KEYSTORE_PASS}",
        },
        "adapters": {
            **{name: {"enabled": False} for name in SHIPPED_ADAPTERS},
            SIMULATOR: {"enabled": settings.simulator},
            "generic": {
                "aliases": {
                    # An alias's ortb-version binds to the enum by name; "2.6" stops Prebid Server at startup.
                    OWN_BOOK: {"enabled": True, "endpoint": f"{pds}/ownbook/openrtb", "ortb-version": "ORTB_2_6"},
                },
            },
        },
        "hooks": {
            MODULE: {
                "enabled": True,
                "policy-url": f"{pds}/policy",
                "policy-timeout-ms": settings.policy_timeout_ms,
                "select-url": f"{pds}/select",
                "select-timeout-ms": settings.select_timeout_ms,
                "tracker-template": settings.tracker_template,
                "log-requests": True,
            },
        },
        "metrics": {"logback": {"enabled": True, "name": "METRICS", "interval": 30}},
        "cache": {"scheme": "https", "host": "${CACHE_HOST}", "path": "/cache", "query": "uuid="},
        "settings": {
            "enforce-valid-account": False,
            "generate-storedrequest-bidrequest-id": True,
            "s3": {
                "endpoint": "${SETTINGS_S3_ENDPOINT}",
                "bucket": "${SETTINGS_S3_BUCKET}",
                "region": "${SETTINGS_S3_REGION}",
                "accounts-dir": "stored_accounts",
                "stored-imps-dir": "stored_impressions",
                "stored-requests-dir": "stored_requests",
                "stored-responses-dir": "stored_responses",
            },
            # The guidance's defaults: a 60 s TTL gave sporadic "Stored request processing failed" 400s (P0-21), so
            # configuration changes go out with a new ECS deployment instead.
            "in-memory-cache": {
                "cache-size": 10000,
                "ttl-seconds": 1200,
                "s3-update": {"refresh-rate": 900000, "timeout": 30000},
            },
        },
        "gdpr": {
            "default-value": 0,
            # The vendor-list cache directories inside the guidance's image.
            "vendorlist": {
                "v2": {"cache-dir": "/var/tmp/vendor2"},  # noqa: S108
                "v3": {"cache-dir": "/var/tmp/vendor3"},  # noqa: S108
            },
        },
        "admin-endpoints": {"logging-changelevel": {"enabled": False}},
        "analytics": {"global": {"adapters": "psdoaAnalytics"}, "psdoa": {"enabled": "${LOG_ANALYTICS_ENABLED}"}},
    }


_CONFIG_HEADER: Final = """\
# Prebid Server configuration of stage {prefix}, rendered by scripts/prebid_guidance.py render-config
# (fast_channels.core.pbs_config). Do not edit: the guidance copies prebid-server/current/ over default/ file for file,
# so this file replaces its default prebid-config.yaml, and a re-render overwrites it.
"""


def render(settings: PbsSettings) -> dict[str, str]:
    """Every file of the stage's configuration, by path relative to the guidance's ``deployment/ecr/prebid-server``
    directory (where the guidance uploads them from at deploy time)."""
    files = {"current-config/prebid-config.yaml": _CONFIG_HEADER.format(prefix=settings.prefix)}
    files["current-config/prebid-config.yaml"] += to_yaml(prebid_config(settings))
    for kids in (False, True):
        for seconds in POD_SECONDS:
            files[f"stored_requests/{stored_request_id(seconds, kids=kids)}.json"] = _json(
                stored_request(settings, seconds, kids=kids)
            )
    files[f"stored_accounts/{settings.prefix}.json"] = _json(account(settings))
    return files


def _json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2) + "\n"


_PLAIN_KEY: Final = re.compile(r"[A-Za-z0-9/][A-Za-z0-9._/-]*")


def to_yaml(data: Mapping[str, Any]) -> str:
    """Block-style YAML of a mapping of mappings, sequences and scalars. Strings are double-quoted (JSON escapes are
    YAML escapes), so no value is ever read back as another type."""
    return "".join(_yaml_lines(data, 0))


def _scalar(value: Any) -> str:
    match value:
        case bool() | None:
            return json.dumps(value)
        case int():
            return str(value)
        case str():
            return json.dumps(value, ensure_ascii=False)
        case dict() if not value:
            return "{}"
        case list() | tuple() if not value:
            return "[]"
    raise TypeError(f"cannot write {type(value).__name__} as YAML")


def _key(key: str) -> str:
    return key if _PLAIN_KEY.fullmatch(key) else json.dumps(key)


def _yaml_lines(data: Mapping[str, Any], indent: int) -> list[str]:
    pad = "  " * indent
    lines = []
    for key, value in data.items():
        match value:
            case dict() if value:
                lines += [f"{pad}{_key(key)}:\n", *_yaml_lines(value, indent + 1)]
            case list() | tuple() if value:
                lines.append(f"{pad}{_key(key)}:\n")
                for item in value:
                    if isinstance(item, dict) and item:
                        first, *rest = _yaml_lines(item, indent + 2)
                        lines += [f"{pad}  - {first.lstrip()}", *rest]
                    else:
                        lines.append(f"{pad}  - {_scalar(item)}\n")
            case _:
                lines.append(f"{pad}{_key(key)}: {_scalar(value)}\n")
    return lines
