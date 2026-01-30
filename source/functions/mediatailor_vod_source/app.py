"""
MediaTailor VOD Source Lambda Function

Creates MediaTailor Channel Assembly VOD sources from MediaPackage VOD assets
and schedules programs to the sample channel.
"""
from __future__ import annotations

import json
import logging
import os
from random import randint
from time import sleep
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.config import Config

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
config = Config(retries={"max_attempts": 3, "mode": "adaptive"})
mediatailor = boto3.client("mediatailor", config=config)
events = boto3.client("events", config=config)
mediapackage_vod = boto3.client("mediapackage-vod", config=config)


def parse_arn(arn: str) -> dict[str, Any]:
    """Parse an AWS ARN into its components."""
    elements = arn.split(":", 5)
    result = {
        "arn": elements[0],
        "partition": elements[1],
        "service": elements[2],
        "region": elements[3],
        "account": elements[4],
        "resource": elements[5],
        "resource_type": None,
    }
    
    if "/" in result["resource"]:
        result["resource_type"], result["resource"] = result["resource"].split("/", 1)
    elif ":" in result["resource"]:
        result["resource_type"], result["resource"] = result["resource"].split(":", 1)
    
    return result


def get_type_from_url(url: str) -> str:
    """Determine HLS or DASH type from manifest URL extension."""
    extension = os.path.splitext(urlparse(url).path)[1]
    
    if extension == ".m3u8":
        return "HLS"
    elif extension == ".mpd":
        return "DASH"
    else:
        raise ValueError(f"Invalid manifest extension: {extension}. Must be .m3u8 or .mpd")


def create_package_configuration(
    manifest_url: str,
    packaging_configuration_id: str,
) -> dict[str, str]:
    """Create a Channel Assembly HTTP package configuration."""
    return {
        "Path": urlparse(manifest_url).path,
        "SourceGroup": packaging_configuration_id,
        "Type": get_type_from_url(manifest_url),
    }


def update_packaging_configurations(vod_source: dict[str, Any]) -> dict[str, Any]:
    """Update existing VOD source with new packaging configurations."""
    new_http_packaging_configuration = vod_source.pop("HttpPackageConfigurations", [None])[0]
    http_packaging_configurations = [new_http_packaging_configuration]

    response = mediatailor.describe_vod_source(**vod_source)
    
    for existing_config in response.get("HttpPackageConfigurations", []):
        if existing_config["SourceGroup"] != new_http_packaging_configuration["SourceGroup"]:
            http_packaging_configurations.append(existing_config)
        else:
            logger.info("Replacing existing packaging configuration: %s", existing_config)

    vod_source["HttpPackageConfigurations"] = http_packaging_configurations
    logger.info("Updating VOD source: %s", json.dumps(vod_source, default=str))
    
    return mediatailor.update_vod_source(**vod_source)


def get_tags(asset_arn: str) -> dict[str, str]:
    """Retrieve tags from a MediaPackage VOD asset."""
    try:
        tags = mediapackage_vod.list_tags_for_resource(ResourceArn=asset_arn).get("Tags", {})
        logger.info("Asset tags: %s", json.dumps(tags))
        return tags
    except Exception as error:
        logger.warning("Error retrieving tags: %s", error)
        return {}


def create_ad_breaks(tags: dict[str, str], source_location: str) -> list[dict[str, Any]]:
    """Create ad break configurations from asset tags."""
    ad_breaks = []
    
    offsets_str = tags.get("AdOffsets", "")
    if not offsets_str:
        return ad_breaks

    offsets = offsets_str.split()
    
    for index, offset in enumerate(offsets):
        ad_breaks.append({
            "OffsetMillis": int(offset),
            "MessageType": "SPLICE_INSERT",
            "SpliceInsertMessage": {
                "AvailNum": index,
                "AvailsExpected": 1,
                "SpliceEventId": index,
                "UniqueProgramId": index,
            },
            "Slate": {
                "SourceLocationName": source_location,
                "VodSourceName": "AdBreakSlate30000",
            },
        })

    return ad_breaks


def lambda_handler(event: dict[str, Any], context: Any) -> str:
    """
    Lambda handler for MediaTailor VOD source creation.
    
    Triggered by MediaPackage VodAssetPlayable events via EventBridge.
    Creates VOD sources and schedules programs to the sample channel.
    """
    logger.debug("Received event: %s", json.dumps(event, default=str))

    source_location = os.environ["MediaTailorSourceLocation"]
    channel_name = os.environ.get("MediaTailorChannelName", "")

    try:
        asset_arn = event["resources"][0]
        vod_source_name = parse_arn(asset_arn)["resource"]
        tags = get_tags(asset_arn)

        vod_source = {
            "VodSourceName": vod_source_name,
            "SourceLocationName": source_location,
            "HttpPackageConfigurations": [
                create_package_configuration(
                    event["detail"]["manifest_urls"][0],
                    event["detail"]["packaging_configuration_id"],
                )
            ],
        }

        if tags:
            logger.info("Adding tags to VOD source: %s", vod_source_name)
            vod_source["Tags"] = tags

        logger.info("Creating VOD source: %s", json.dumps(vod_source, default=str))
        response = mediatailor.create_vod_source(**vod_source)

    except mediatailor.exceptions.BadRequestException as error:
        if "exists" in error.response["Error"]["Message"]:
            logger.info("VOD source exists, updating packaging configuration")
            sleep(randint(10, 100) / 10)
            
            if tags:
                del vod_source["Tags"]  # update_vod_source doesn't support tags
            
            update_packaging_configurations(vod_source)
            response = mediatailor.describe_vod_source(
                VodSourceName=vod_source["VodSourceName"],
                SourceLocationName=vod_source["SourceLocationName"],
            )
        else:
            raise

    # Create program in sample channel if configured
    if channel_name:
        try:
            sleep(5)  # Allow VOD source to propagate
            logger.info("Creating program in sample channel: %s", channel_name)

            schedule = mediatailor.get_channel_schedule(
                ChannelName=channel_name,
                DurationMinutes="1",
                MaxResults=1,
            )

            program = {
                "ChannelName": channel_name,
                "ProgramName": vod_source_name,
                "SourceLocationName": source_location,
                "VodSourceName": vod_source_name,
                "ScheduleConfiguration": {
                    "Transition": {
                        "Type": "RELATIVE",
                        "RelativePosition": "BEFORE_PROGRAM",
                    }
                },
            }

            # Set relative program if schedule has existing items
            if schedule.get("Items"):
                program["ScheduleConfiguration"]["Transition"]["RelativeProgram"] = (
                    schedule["Items"][0]["ProgramName"]
                )

            # Add ad breaks if configured
            ad_breaks = create_ad_breaks(tags, source_location)
            if ad_breaks:
                program["AdBreaks"] = ad_breaks

            logger.info("Creating program: %s", json.dumps(program, default=str))
            response = mediatailor.create_program(**program)

        except mediatailor.exceptions.BadRequestException as error:
            if "exists" in error.response["Error"]["Message"]:
                logger.info("Program exists, recreating")
                mediatailor.delete_program(
                    ChannelName=channel_name,
                    ProgramName=vod_source_name,
                )
                sleep(5)
                response = mediatailor.create_program(**program)
            else:
                logger.error("Error creating program: %s", error)

        except Exception as error:
            logger.error("Unexpected error creating program: %s", error)

    return json.dumps(response, default=str)
