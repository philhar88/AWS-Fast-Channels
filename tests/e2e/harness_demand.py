"""Building blocks of the demand leg's tests (``test_11_demand_edge.py``): the guidance's edge as MediaTailor reaches
it, read only through the stage's SSM contract ``/<prefix>/demand/*`` and origin secret A."""

from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Any, Final

import httpx

from fast_channels.core.demand import Parameter, origin_secret, parameter_path, simulator_function
from fast_channels.core.pbs_config import auction_body

if TYPE_CHECKING:
    import boto3

TIMEOUT_S: Final = 10
EDGE_REGION: Final = "us-east-1"
"""Where DemandEdge keeps the origin secrets."""
SID: Final = "e2e-demand"


@dataclass
class DemandEdge:
    """A stage's demand module, from the outside."""

    prefix: str
    session: boto3.Session

    @cached_property
    def parameters(self) -> dict[str, str]:
        ssm = self.session.client("ssm")
        pages = ssm.get_paginator("get_parameters_by_path").paginate(Path=parameter_path(self.prefix))
        return {p["Name"].rsplit("/", 1)[1]: p["Value"] for page in pages for p in page["Parameters"]}

    @property
    def deployed(self) -> bool:
        return Parameter.CLOUDFRONT_DOMAIN.value in self.parameters

    @property
    def domain(self) -> str:
        return self.parameters[Parameter.CLOUDFRONT_DOMAIN.value]

    @cached_property
    def secret(self) -> str:
        secrets = self.session.client("secretsmanager", region_name=EDGE_REGION)
        return str(secrets.get_secret_value(SecretId=origin_secret(self.prefix, "a"))["SecretString"])

    def auction(
        self,
        seconds: int,
        *,
        secret: bool = True,
        account: str | None = None,
        headers: dict[str, str] | None = None,
        sid: str = SID,
        evid: str = "1",
        upid: str = "e2e",
        probe: bool = True,
    ) -> httpx.Response:
        """MediaTailor's POST for a ``seconds`` break: the stored request and the break's length, the session and
        configuration as headers, by default a probe (so the Pod Decision Service answers house only)."""
        body: dict[str, Any] = auction_body(seconds)
        if account is not None:
            body["app"] = {"publisher": {"id": account}}
        sent = {
            "Content-Type": "application/json",
            "X-Fast-Sid": sid,
            "X-Fast-Pc": f"{self.prefix}-e2e",
            **({"X-Fast-Probe": "1"} if probe else {}),
            **({"X-Fast-Origin": self.secret} if secret else {}),
            **(headers or {}),
        }
        query = f"evid={evid}&upid={upid}&dur={seconds * 1001}"
        return httpx.post(f"https://{self.domain}/openrtb2/auction?{query}", json=body, headers=sent, timeout=TIMEOUT_S)

    def get(self, path: str) -> httpx.Response:
        return httpx.get(f"https://{self.domain}{path}", timeout=TIMEOUT_S)

    def simulator_requests(self, since_ms: int) -> int:
        """Bid requests the ``lambda`` simulator logged since ``since_ms`` (0 when it does not exist)."""
        logs = self.session.client("logs")
        group = f"/aws/lambda/{simulator_function(self.prefix)}"
        try:
            pages = logs.get_paginator("filter_log_events").paginate(
                logGroupName=group, startTime=since_ms, filterPattern='"sim-bid-request"'
            )
            return sum(len(page["events"]) for page in pages)
        except logs.exceptions.ResourceNotFoundException:
            return 0
