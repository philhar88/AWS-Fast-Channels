"""The edge rebuild (ADR-0018, DESIGN-v2.2 §5.2): a CloudFront Function on the media distribution's ``/v1/*``
behaviour, viewer request, so that nothing the viewer can change reaches MediaTailor's session init but a single
well-formed playback ticket (``edge_rebuild.js``).

- The ``/v1/*`` behaviour then allows GET, HEAD and OPTIONS only: ``POST /v1/session`` gets CloudFront's 403, and the
  function refuses ``GET /v1/session`` (403) and a non-canonical init path (400).
- A function that throws makes CloudFront answer 503 (``FunctionExecutionError``): the function catches every error,
  and :func:`edge_error_alarm` fires on the first ``FunctionExecutionErrors`` it counts.
- CloudFront's standard logs record the viewer's original query, before the rewrite, so with the module on the
  distribution's access logs leave out ``cs-uri-query`` (and the viewer's address, ``c-ip`` and ``x-forwarded-for``,
  which the session id in the path would otherwise tie to a viewer id).
"""

from pathlib import Path
from typing import Final

from aws_cdk import Duration
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_sns as sns
from constructs import Construct

from fast_channels.infra.constructs.alerts import notify

CODE: Final = Path(__file__).with_name("edge_rebuild.js")
"""The function's source (cloudfront-js-2.0)."""
NAMESPACE: Final = "AWS/CloudFront"
REGION_DIMENSION: Final = "Global"
"""CloudFront Functions' metrics are published in us-east-1 with ``Region=Global``."""


def function_name(prefix: str) -> str:
    """``<prefix>-identity-edge``: CloudFront Function names are unique per account."""
    return f"{prefix}-identity-edge"


class EdgeRebuild(Construct):
    """The edge rebuild function (module docstring), published on every deployment.

    Attributes:
        function: the CloudFront Function, associated with the ``/v1/*`` behaviour as a viewer-request function.
    """

    def __init__(self, scope: Construct, id: str, *, prefix: str) -> None:
        super().__init__(scope, id)
        self.function = cf.Function(
            self,
            "Function",
            function_name=function_name(prefix),
            comment="FAST Channels identity: MediaTailor init gets only a valid ads.t; /v1/session refused",
            code=cf.FunctionCode.from_file(file_path=str(CODE)),
            runtime=cf.FunctionRuntime.JS_2_0,
            auto_publish=True,
        )


def edge_error_alarm(scope: Construct, id: str, *, prefix: str, topic: sns.ITopic, name: str) -> cw.Alarm:
    """Alarm on any ``FunctionExecutionErrors`` of the edge rebuild: each one is a viewer's 503. CloudFront publishes
    the metric in us-east-1 only, so create it in a us-east-1 stack (Media there, otherwise Edge)."""
    created = cw.Alarm(
        scope,
        id,
        alarm_name=name,
        alarm_description="The identity edge rebuild failed: each error is a 503 to a viewer (docs/identity.md)",
        metric=cw.Metric(
            namespace=NAMESPACE,
            metric_name="FunctionExecutionErrors",
            dimensions_map={"FunctionName": function_name(prefix), "Region": REGION_DIMENSION},
            statistic=cw.Stats.SUM,
            period=Duration.minutes(1),
        ),
        threshold=0,
        evaluation_periods=1,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )
    notify(created, topic)
    return created
