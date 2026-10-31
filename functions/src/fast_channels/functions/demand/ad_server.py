"""The demo's mock ad server (``modules.demand.ad_server: mock``, M8-09): ``GET /ads``, a GAM-like line-item match.

MediaTailor calls it as the ad-server siblings' ADS with the break's Prebid targeting as key-values
(``core.ad_server.ads_url``). A Prebid line matched on ``hb_pb`` answers a VAST Wrapper to the pod cached in Prebid
Cache (``<cache>?uuid=<hb_uuid>``); anything else, the house line's Wrapper to the stage's house pod
(``core.ad_server.mock_answer``).

It sits behind an API Gateway HTTP API, and it answers only the stage's own MediaTailor requests: they carry the origin
secret in ``X-Fast-Origin``, as the demand origin's rule requires for Prebid Server. Both slots are valid at once (a
rotation never blocks a request), read from Secrets Manager and kept 5 minutes. A value that matches neither is
checked once more against a fresh read, at most every 10 s, so the value a rotation switches the siblings to is
accepted at once. Anything else is 403, and nothing is logged about it beyond a count. Each answer is logged with
its line, the session and the break, never the secret.
"""

import hmac
import os
import time
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.parameters import SecretsProvider
from botocore.config import Config

from fast_channels.core.ad_server import mock_answer
from fast_channels.core.demand import ORIGIN_HEADER

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
metrics = Metrics()
app = APIGatewayHttpResolver()

SECRET_SECONDS: Final = 300
"""How long a read origin secret is kept."""
REFRESH_SECONDS: Final = 10
"""At most one fresh read per this many seconds for a value that matches neither kept secret (a rotation's new one,
or a guess: guesses cannot make the function read Secrets Manager more often)."""
VAST: Final = "application/xml"


def _settings() -> dict[str, str]:
    return {
        "cache_url": os.environ["CACHE_URL"],
        "house_url": os.environ["HOUSE_URL"],
        "impression_url": os.environ["IMPRESSION_URL"],
    }


_secrets = SecretsProvider(boto_config=Config(region_name=os.environ.get("ORIGIN_SECRET_REGION")))


_refreshed: list[float] = [0.0]
"""When the secrets were last read fresh for an unknown value (monotonic seconds)."""


def origin_secrets(*, fresh: bool = False) -> list[str]:
    """The origin secrets the stage's MediaTailor requests may carry (both slots); ``fresh`` reads them again."""
    names = [name for name in os.environ.get("ORIGIN_SECRETS", "").split(",") if name]
    return [str(_secrets.get(name, max_age=SECRET_SECONDS, force_fetch=fresh)) for name in names]


def _matches(value: str, secrets: list[str]) -> bool:
    return any(hmac.compare_digest(value, secret) for secret in secrets if secret)


def authorized(value: str | None) -> bool:
    """Whether ``value`` is one of the origin secrets (compared in constant time), read again at most every
    :data:`REFRESH_SECONDS` when it matches neither kept one."""
    if not value:
        return False
    if _matches(value, origin_secrets()):
        return True
    if time.monotonic() - _refreshed[0] < REFRESH_SECONDS:
        return False
    _refreshed[0] = time.monotonic()
    return _matches(value, origin_secrets(fresh=True))


@app.get("/ads")
def ads() -> Response[str]:
    """The decision for one break: the Prebid line's Wrapper to the cached pod, or the house line's."""
    if not authorized(app.current_event.headers.get(ORIGIN_HEADER)):
        metrics.add_metric(name="AdServerRefused", unit=MetricUnit.Count, value=1)
        return Response(status_code=403, content_type="text/plain", body="")
    query: dict[str, str] = dict(app.current_event.query_string_parameters or {})
    logger.set_correlation_id(f"{query.get('sid', '')}.{query.get('evid', '')}")
    line, vast = mock_answer(query, **_settings())
    logger.info(
        "ad decision",
        extra={
            "line": line,
            "pc": query.get("pc", ""),
            "sid": query.get("sid", ""),
            "evid": query.get("evid", ""),
            "dur_ms": query.get("dur", ""),
            "hb_pb": query.get("hb_pb", ""),
            "hb_bidder": query.get("hb_bidder", ""),
            "uuid8": query.get("hb_uuid", "")[:8],
        },
    )
    metrics.add_metric(
        name="AdServerPrebidLine" if line.startswith("prebid-") else "AdServerHouseLine", unit=MetricUnit.Count, value=1
    )
    return Response(status_code=200, content_type=VAST, body=vast, headers={"Cache-Control": "no-store"})


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)
