# pylint: disable=unused-import,expression-not-assigned,pointless-statement

from diagrams import Cluster, Diagram,Edge
from diagrams.aws.compute import ECS, EKS, Lambda
from diagrams.aws.database import Redshift
from diagrams.aws.integration import SQS
from diagrams.aws.storage import S3
from diagrams.aws.network import CloudFront
from diagrams.aws.general import Users
from diagrams.aws.media import ElementalMediaconvert, ElementalMediapackage, ElementalMediatailor

with Diagram("FAST on AWS", show=False):

    queue = SQS("event queue")

    with Cluster("Media Preperation"):
        source = S3("Source Bucket")
        with Cluster("Encoding"):
            lamba_emc = Lambda("Create MediaConvert Job")
            encode = ElementalMediaconvert("Create HLS ABR")

        destination = S3("Destination Bucket")

    with Cluster("Media Origination"):
        lambda_emp = Lambda("Create MediaPackage Asset")
        package = ElementalMediapackage("Ingest Vod Asset")
        lambda_emt = Lambda("Create MediaTailor Source")
        vodsource = ElementalMediatailor("Create Vod Source")

    with Cluster("Media Distribution"):
        cloudfront = CloudFront("CDN")
        channel = ElementalMediatailor("SampleChannel")
        emt = ElementalMediatailor("Ad Insertion")
        users = Users("Users")

    #Events
    source \
        >> Edge(label="file upload event", style="dashed") >> queue
    queue \
        >> Edge(label="file upload event", style="dashed") \
        >> lamba_emc >> Edge(label="Job JSON", style="dashed") \
        >> encode >> Edge(label="Job Complete Event", style="dashed") >> queue
    queue \
        >> Edge(label="Job Complete Event", style="dashed") \
        >> lambda_emp >> Edge(label="Ingest Asset API", style="dashed") \
        >> package >> Edge(label="Asset Playable Event", style="dashed") \
        >> queue
    queue \
        >> Edge(label="Asset Playableƒ Event", style="dashed") \
        >> lambda_emt >> Edge(label="CreateSourceLocation API", style="dashed") >> vodsource

    # Files
    encode >> Edge(label="hls media files") >> destination
    source >> Edge(label="mezzanine media files") >> encode
    package >> Edge(label="hls media files") >> destination

    cloudfront >> Edge(label="media segments") >> package
    cloudfront >> Edge(label="manifests") >> channel
    emt >> Edge(label="manifests") >> cloudfront
    users >> Edge(label="manifests") >> emt
    users >> Edge(label="media segments") >> cloudfront