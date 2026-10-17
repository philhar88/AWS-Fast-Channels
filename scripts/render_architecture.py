#!/usr/bin/env -S uv run --group docs python
"""Render docs/images/architecture.png with the official AWS architecture icons.

uv run --group docs python scripts/render_architecture.py      # needs Graphviz (`dot`) on PATH
"""

from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.devtools import XRay
from diagrams.aws.general import Users
from diagrams.aws.integration import Eventbridge, EventbridgeScheduler, StepFunctions
from diagrams.aws.management import Cloudwatch
from diagrams.aws.media import ElementalMediaconvert, ElementalMediatailor
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import CloudFront
from diagrams.aws.security import Cognito
from diagrams.aws.storage import S3

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "images" / "architecture"

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
        viewers = Users("Viewers\n(SSAI / SGAI players)")
        operators = Users("Operators")

        with Cluster("Ingest & encoding"):
            ingest_bucket = S3("Ingest bucket")
            pipeline = StepFunctions("Ingest pipeline\n(probe → encode →\nfinalize → register)")
            mediaconvert = ElementalMediaconvert("MediaConvert\none HLS encode\nMPEG-TS: AVC + AAC muxed\n(Basic tier)")
            media_bucket = S3("Media bucket\n(HLS playlists,\nTS segments)")
            library = Dynamodb("Media library\n(assets, ladders,\nchannels, EPG)")

        with Cluster("Linear channels"):
            channel_assembly = ElementalMediatailor("MediaTailor\nChannel Assembly\n(LINEAR, S3 SigV4 origin)")
            ad_insertion = ElementalMediatailor("MediaTailor\nad insertion\nSSAI + SGAI")

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


if __name__ == "__main__":
    main()
