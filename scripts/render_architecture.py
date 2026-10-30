#!/usr/bin/env -S uv run --group docs python
"""Render the architecture diagrams with the official AWS architecture icons.

uv run --group docs python scripts/render_architecture.py      # needs Graphviz (`dot`) on PATH

- ``docs/images/architecture.png``: the deployment with every module off (v3.0.0's).
- ``docs/images/architecture-modules.png``: the optional modules (docs/modules.md), drawn around the channel's
  monetized sibling configurations. The experimental ad-server chain is dashed and labelled.
"""

from pathlib import Path
from typing import Any

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.analytics import Athena, KinesisDataFirehose
from diagrams.aws.compute import ElasticContainerServiceContainer, Lambda
from diagrams.aws.database import Dynamodb, ElastiCache
from diagrams.aws.devtools import XRay
from diagrams.aws.general import Users
from diagrams.aws.integration import Eventbridge, EventbridgeScheduler, StepFunctions
from diagrams.aws.management import Cloudwatch, SystemsManagerAppConfig
from diagrams.aws.media import ElementalMediaconvert, ElementalMediatailor
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import APIGateway, CloudFront, ElbApplicationLoadBalancer
from diagrams.aws.security import WAF, Cognito
from diagrams.aws.storage import S3

IMAGES = Path(__file__).resolve().parents[1] / "docs" / "images"
OUTPUT = IMAGES / "architecture"
MODULES_OUTPUT = IMAGES / "architecture-modules"

GRAPH = {
    "fontname": "Helvetica",
    "fontsize": "22",
    "pad": "0.4",
    "nodesep": "0.55",
    "ranksep": "0.9",
    "splines": "spline",
}
NODE = {"fontname": "Helvetica", "fontsize": "12"}
EDGE = {"fontname": "Helvetica", "fontsize": "11", "color": "#545B64"}
MEDIA = {"color": "#E7157B", "style": "bold"}
AGENT = {"color": "#01A88D", "style": "bold"}
ADS = {"color": "#ED7100", "style": "dashed"}
DEMAND = {"color": "#ED7100", "style": "bold"}
RELEASE = {"color": "#7AA116", "style": "bold"}
RECORDS = {"color": "#8C4FFF", "style": "dotted"}
EXPERIMENTAL = {"color": "#ED7100", "style": "dashed"}
EXPERIMENTAL_CLUSTER = {"style": "dashed", "pencolor": "#ED7100", "fontcolor": "#ED7100"}


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with Diagram(
        "FAST Channels on AWS",
        filename=str(OUTPUT),
        outformat="png",
        show=False,
        direction="LR",
        graph_attr=GRAPH,
        node_attr=NODE,
        edge_attr=EDGE,
    ):
        content_owner = Users("Content owner\n/ CMS")
        viewers = Users("Viewers\n(HLS players)")
        operators = Users("Operators")

        with Cluster("Ingest & encoding"):
            ingest_bucket = S3("Ingest bucket")
            pipeline = StepFunctions("Ingest pipeline\n(probe → encode →\nfinalize → register)")
            mediaconvert = ElementalMediaconvert(
                "MediaConvert\none HLS encode\nMPEG-TS: AVC + AAC muxed\n(Basic tier; Professional\nwith ESAM cues)"
            )
            media_bucket = S3("Media bucket\n(HLS playlists,\nTS segments)")
            library = Dynamodb("Media library\n(assets, ladders,\nchannels, EPG)")

        with Cluster("Linear channels"):
            channel_assembly = ElementalMediatailor("MediaTailor\nChannel Assembly\n(LINEAR, S3 SigV4 origin)")
            ad_insertion = ElementalMediatailor("MediaTailor\nad insertion\nSSAI (stitched)")

        with Cluster("Delivery"):
            cdn = CloudFront("CloudFront\n(OAC, ordered\nMediaTailor behaviours)")

        with Cluster("Operator plane — Amazon Bedrock AgentCore"):
            identity = Cognito("Cognito\n(operator groups)")
            runtime = Bedrock("AgentCore Runtime\nscheduling agent\n(Strands, AG-UI)")
            gateway = Bedrock("AgentCore Gateway\nMCP tools +\nCedar policy")
            memory = Bedrock("AgentCore\nMemory")
            tools = Lambda("Tool Lambdas\ncatalog · library · origins\nschedule · channels · encoding")
            autopilot = EventbridgeScheduler("Autopilot\nschedules")

        with Cluster("Observability — every service logs to CloudWatch"):
            events = Eventbridge("Domain events")
            logs = Cloudwatch("CloudWatch logs,\nmetrics, dashboard")
            traces = XRay("X-Ray /\nTransaction Search")

        content_owner >> Edge(label="mezzanine + metadata", **MEDIA) >> ingest_bucket >> Edge(**MEDIA) >> pipeline
        pipeline >> Edge(**MEDIA) >> mediaconvert >> Edge(label="HLS", **MEDIA) >> media_bucket
        pipeline >> Edge(label="assets", **MEDIA) >> library
        media_bucket >> Edge(label="manifests (SigV4)", **MEDIA) >> channel_assembly
        channel_assembly >> Edge(label="SigV4-only\nchannel policy", **MEDIA) >> ad_insertion
        ad_insertion >> Edge(label="personalised\nmanifests", **MEDIA) >> cdn
        media_bucket >> Edge(label="segments (OAC)", **MEDIA) >> cdn
        ad_insertion >> Edge(label="VAST pods", **ADS) >> media_bucket
        cdn >> Edge(**MEDIA) >> viewers

        operators >> Edge(label="console / chat", **AGENT) >> identity >> Edge(**AGENT) >> runtime
        autopilot >> Edge(label="M2M token", **AGENT) >> runtime
        runtime >> Edge(**AGENT) >> memory
        runtime >> Edge(label="operator's JWT", **AGENT) >> gateway >> Edge(**AGENT) >> tools
        tools >> Edge(label="programs, sources,\nchannels", **AGENT) >> channel_assembly
        tools >> Edge(label="ladders, ingest", **AGENT) >> pipeline
        tools >> Edge(**AGENT) >> library

        pipeline >> Edge(label="AssetReady / Failed", style="dotted") >> events
        events >> Edge(style="invis") >> logs >> Edge(style="invis") >> traces


def modules() -> None:
    """The optional modules around one monetized channel (docs/modules.md)."""
    with Diagram(
        "FAST Channels on AWS: the optional modules",
        filename=str(MODULES_OUTPUT),
        outformat="png",
        show=False,
        direction="LR",
        graph_attr=GRAPH,
        node_attr=NODE,
        edge_attr=EDGE,
    ):
        _module_edges(_module_nodes())


def _module_nodes() -> dict[str, Any]:
    nodes: dict[str, Any] = {"viewers": Users("Viewers")}
    with Cluster("v3.0.0 media plane"):
        nodes["channel"] = ElementalMediatailor("Channel Assembly\nchannel")
        nodes["cdn"] = CloudFront("Stage CloudFront\n(segments, /catalog/ctx/*)")
    with Cluster("release: Release Controller"):
        nodes["siblings"] = ElementalMediatailor(
            "Sibling playback\nconfigurations <channel>-p-a/b/z\n(MediaTailor Functions:\nPRE_SESSION, PRE_ADS)"
        )
        nodes["controller"] = StepFunctions("Apply, smoke gate,\nrollback (R1),\nsecret rotation")
        nodes["pointer"] = SystemsManagerAppConfig("Release pointer\n(gradual promotion)")
        nodes["prefetcher"] = Lambda("Prefetcher\n(one SINGLE schedule\nper upcoming break)")
    with Cluster("demand: Prebid Server guidance (its own VPC)"):
        nodes["edge"] = WAF("Origin rule\n(origin secret)")
        nodes["guidance_cdn"] = CloudFront("Guidance\nCloudFront")
        nodes["alb"] = ElbApplicationLoadBalancer("Load balancer")
        with Cluster("Prebid Server task (ECS Fargate, census-gated)"):
            nodes["prebid"] = ElasticContainerServiceContainer("Prebid Server 4.4.0\n+ fast-ctv module")
            nodes["sidecar"] = ElasticContainerServiceContainer(
                "pds: Pod Decision Service\nsidecar (/policy, /select,\nown book)"
            )
        nodes["bidders"] = Users("Bidders\n(simulator in demo)")
    with Cluster("pds state"):
        nodes["pds_table"] = Dynamodb("Budgets, reservations,\ndeliveries")
        nodes["delivery"] = Lambda("Delivery\n(our tracker's\nimpressions)")
    with Cluster("crawl"):
        nodes["publisher"] = Lambda("Context publisher")
        nodes["contexts"] = S3("Catalog context\nobjects")
    with Cluster("ops: lake and ledger"):
        nodes["firehose"] = KinesisDataFirehose("Decision records,\nlog copies")
        nodes["lake"] = Athena("Lake (Athena)")
        nodes["ledger"] = Dynamodb("Hash-chained\nledger")
    with Cluster("Experimental (off by default): an ad server in front", graph_attr=EXPERIMENTAL_CLUSTER):
        nodes["ad_server"] = APIGateway("Ad server\n(mock; GAM, FreeWheel)")
        nodes["prebid_cache"] = ElastiCache("Prebid Cache\n(the guidance's)")
    return nodes


def _module_edges(n: dict[str, Any]) -> None:
    siblings, prebid, sidecar = n["siblings"], n["prebid"], n["sidecar"]
    n["viewers"] >> Edge(label="SSAI sessions", **DEMAND) >> n["cdn"] >> Edge(**DEMAND) >> siblings
    n["channel"] >> Edge(label="channel origin", **DEMAND) >> siblings
    siblings >> Edge(label="templated OpenRTB POST\n(+ origin secret)", **DEMAND) >> n["guidance_cdn"]
    n["guidance_cdn"] >> Edge(**DEMAND) >> n["edge"] >> Edge(**DEMAND) >> n["alb"] >> Edge(**DEMAND) >> prebid
    prebid >> Edge(label="localhost\n/policy, /select", **DEMAND) >> sidecar
    sidecar >> Edge(label="reserve", **DEMAND) >> n["pds_table"]
    prebid >> Edge(label="OpenRTB", **DEMAND) >> n["bidders"]
    prebid >> Edge(label="one VAST pod", **DEMAND) >> siblings

    n["controller"] >> Edge(label="configure siblings,\nput Functions", **RELEASE) >> siblings
    n["controller"] >> Edge(**RELEASE) >> n["pointer"]
    n["pointer"] >> Edge(label="stable sibling", **RELEASE) >> siblings
    n["prefetcher"] >> Edge(label="prefetch schedules", **RELEASE) >> siblings
    n["publisher"] >> Edge(label="title metadata", **RELEASE) >> n["contexts"] >> Edge(**RELEASE) >> n["cdn"]
    siblings >> Edge(label="PRE_ADS lookup\nby UPID", **RELEASE) >> n["cdn"]

    siblings >> Edge(label="ADS logs", **RECORDS) >> n["delivery"] >> Edge(**RECORDS) >> n["pds_table"]
    sidecar >> Edge(label="decision records", **RECORDS) >> n["firehose"] >> Edge(**RECORDS) >> n["lake"]
    n["controller"] >> Edge(label="every release", **RECORDS) >> n["ledger"]
    n["delivery"] >> Edge(label="money events", **RECORDS) >> n["ledger"]

    siblings >> Edge(label="ad request with hb_pb,\nhb_uuid (experimental)", **EXPERIMENTAL) >> n["ad_server"]
    prebid >> Edge(label="cache the pod", **EXPERIMENTAL) >> n["prebid_cache"]
    n["ad_server"] >> Edge(label="Wrapper to /cache", **EXPERIMENTAL) >> n["prebid_cache"]


if __name__ == "__main__":
    main()
    modules()
