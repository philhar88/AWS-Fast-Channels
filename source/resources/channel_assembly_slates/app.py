"""
MediaConvert Slates Custom Resource

CloudFormation custom resource for creating ad break slate videos using MediaConvert.
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any

import boto3
from botocore.config import Config
from crhelper import CfnResource

# Configure logging
boto_level = os.environ.get("LOG_LEVEL", "INFO").upper()
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

logger = logging.getLogger(__name__)
logger.setLevel(log_level)

# Initialize CfnResource helper
helper = CfnResource(
    json_logging=False,
    log_level=log_level,
    boto_level=boto_level,
    sleep_on_delete=120,
    ssl_verify=None,
)

# Initialize AWS clients
config = Config(retries={"max_attempts": 3, "mode": "adaptive"})

try:
    mediaconvert_client = boto3.client("mediaconvert", config=config)
    endpoint = mediaconvert_client.describe_endpoints()["Endpoints"][0]["Url"]
    logger.info("MediaConvert endpoint URL: %s", endpoint)
    mediaconvert = boto3.client("mediaconvert", endpoint_url=endpoint, config=config)
    mediapackage = boto3.client("mediapackage-vod", config=config)
    mediatailor = boto3.client("mediatailor", config=config)
    s3 = boto3.resource("s3")
except Exception as e:
    helper.init_failure(e)


def generate_settings_from_preset(preset_name: str) -> dict[str, Any]:
    """Generate output settings from a MediaConvert preset."""
    preset = mediaconvert.get_preset(Name=preset_name)["Preset"]["Settings"]
    logger.info("Preset settings: %s", json.dumps(preset, default=str))
    
    if "VideoDescription" in preset:
        codec_settings = preset["VideoDescription"]["CodecSettings"]
        codec = codec_settings.get("Codec")
        
        framerate_settings = {
            "FramerateControl": "SPECIFIED",
            "FramerateNumerator": 30,
            "FramerateDenominator": 1,
        }
        
        if codec == "H_265":
            codec_settings["H265Settings"].update(framerate_settings)
        elif codec == "H_264":
            codec_settings["H264Settings"].update(framerate_settings)
        else:
            raise ValueError(f"Unknown codec: {codec}. Must be H_264 or H_265")
    
    return preset


def format_template_for_slate(
    template: dict[str, Any],
    duration_millis: int,
    slate_name: str,
    transcode_role_arn: str,
) -> dict[str, Any]:
    """Format a MediaConvert job template for slate video generation."""
    job_settings = {
        "Role": transcode_role_arn,
        "Settings": {
            **template["Settings"],
            "Inputs": [
                {
                    "VideoGenerator": {"Duration": duration_millis},
                    "AudioSelectors": {
                        "Audio Selector 1": {"DefaultSelection": "DEFAULT"}
                    },
                }
            ],
        },
    }
    
    # Update output group settings
    output_group = job_settings["Settings"]["OutputGroups"][0]
    hls_settings = output_group["OutputGroupSettings"]["HlsGroupSettings"]
    hls_settings["Destination"] += slate_name
    hls_settings["SegmentLength"] = 2
    
    # Update outputs with preset settings
    for output in output_group["Outputs"]:
        logger.info("Processing output: %s", json.dumps(output, default=str))
        output.update(generate_settings_from_preset(output["Preset"]))

    logger.info("Formatted job settings: %s", json.dumps(job_settings, default=str))
    return job_settings


@helper.create
@helper.update
def create(event: dict[str, Any], context: Any) -> str:
    """Handle CloudFormation Create/Update event."""
    logger.info("Processing Create/Update request")
    
    properties = event["ResourceProperties"]
    physical_resource_id = properties.get("Name", f"AdBreakSlate_{properties['SlateDurationInMillis']}")

    slate_duration_millis = int(properties["SlateDurationInMillis"])
    transcode_role_arn = properties["MediaConvertTranscodeRoleArn"]
    
    template = mediaconvert.get_job_template(
        Name=properties["MediaConvertJobTemplate"]["Name"]
    )["JobTemplate"]
    
    job_settings = format_template_for_slate(
        template, slate_duration_millis, physical_resource_id, transcode_role_arn
    )
    
    job = mediaconvert.create_job(**job_settings)["Job"]
    result = {"Status": job["Status"], "Id": job["Id"]}
    logger.info("Created job: %s", json.dumps(result))
    
    # Wait for job completion
    while result["Status"] in ("SUBMITTED", "PROGRESSING"):
        time.sleep(5)
        result["Status"] = mediaconvert.get_job(Id=result["Id"])["Job"]["Status"]
        logger.info("Job status: %s", result["Status"])
    
    if result["Status"] == "ERROR":
        raise ValueError(f"MediaConvert job {result['Id']} failed. Check MediaConvert console.")

    return physical_resource_id


@helper.delete
def delete(event: dict[str, Any], context: Any) -> None:
    """Handle CloudFormation Delete event."""
    logger.info("Processing Delete request")
    
    physical_resource_id = event["PhysicalResourceId"]
    properties = event["ResourceProperties"]

    # Delete VOD sources from MediaTailor
    try:
        vod_sources = mediatailor.list_vod_sources(
            SourceLocationName=physical_resource_id,
            MaxResults=100,
        )
        
        for vod_source in vod_sources.get("Items", []):
            if "AdBreakSlate_" in vod_source["VodSourceName"]:
                try:
                    mediatailor.delete_vod_source(
                        SourceLocationName=physical_resource_id,
                        VodSourceName=vod_source["VodSourceName"],
                    )
                    logger.info("Deleted VOD source: %s", vod_source["VodSourceName"])
                except Exception as error:
                    logger.warning("Failed to delete VOD source: %s", error)
    except Exception as error:
        logger.warning("Error listing VOD sources: %s", error)

    # Delete assets from MediaPackage
    if "MediaPackagePackagingGroup" in properties:
        try:
            assets = mediapackage.list_assets(
                PackagingGroupId=properties["MediaPackagePackagingGroup"]["Id"],
                MaxResults=100,
            )
            
            for asset in assets.get("Assets", []):
                if "AdBreakSlate_" in asset["Id"]:
                    try:
                        mediapackage.delete_asset(Id=asset["Id"])
                        logger.info("Deleted asset: %s", asset["Id"])
                    except Exception as error:
                        logger.warning("Failed to delete asset %s: %s", asset["Id"], error)
        except mediapackage.exceptions.NotFoundException:
            logger.info("Packaging group not found")
        except Exception as error:
            logger.warning("Error deleting MediaPackage assets: %s", error)

    # Delete S3 objects
    try:
        bucket = s3.Bucket(properties["VideoDestinationBucket"])
        bucket.objects.filter(Prefix=physical_resource_id).delete()
        logger.info("Deleted S3 objects with prefix: %s", physical_resource_id)
    except Exception as error:
        logger.warning("Error deleting S3 objects: %s", error)


def lambda_handler(event: dict[str, Any], context: Any) -> None:
    """Lambda entry point for CloudFormation custom resource."""
    logger.info("Received event: %s", json.dumps(event, default=str))
    
    # Stagger completion to avoid resource conflicts
    time.sleep(random.randint(1, 10))
    
    helper(event, context)
