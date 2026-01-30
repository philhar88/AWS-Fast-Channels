"""
MediaPackage VOD Asset Lambda Function

Creates MediaPackage VOD assets from MediaConvert job outputs
and generates playback URLs for MediaTailor SSAI.
"""
from __future__ import annotations

import json
import logging
import os
import re
from random import randint
from time import sleep
from typing import Any
from urllib.parse import urlparse, urlunparse

import boto3
from botocore.config import Config

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
config = Config(retries={"max_attempts": 3, "mode": "adaptive"})
mediapackage = boto3.client("mediapackage-vod", config=config)
s3 = boto3.client("s3", config=config)
events = boto3.client("events", config=config)


def generate_playback_urls(
    egress_endpoints: list[dict[str, Any]],
    asset_id: str,
    asset_tags: dict[str, str],
) -> list[dict[str, Any]]:
    """Generate MediaTailor SSAI playback URLs for a given asset."""
    playback_urls = []

    try:
        mediatailor_playback_endpoints = {
            "Hls": urlparse(os.environ.get("MediaTailorPlaybackConfigurationVodHls", "")),
            "Dash": urlparse(os.environ.get("MediaTailorPlaybackConfigurationVodDash", "")),
        }
    except KeyError:
        logger.warning("MediaTailor environment variables not configured")
        mediatailor_playback_endpoints = {"Hls": "", "Dash": ""}

    for egress_endpoint in egress_endpoints:
        url = urlparse(egress_endpoint["Url"])
        packaging_configuration_id = egress_endpoint["PackagingConfigurationId"]
        
        extension = os.path.splitext(url.path)[1]
        
        if extension == ".m3u8" and mediatailor_playback_endpoints["Hls"]:
            hls_endpoint = mediatailor_playback_endpoints["Hls"]
            url = url._replace(
                netloc=hls_endpoint.netloc,
                path=hls_endpoint.path.rstrip("/") + url.path,
            )
        elif extension == ".mpd" and mediatailor_playback_endpoints["Dash"]:
            dash_endpoint = mediatailor_playback_endpoints["Dash"]
            url = url._replace(
                netloc=dash_endpoint.netloc,
                path=dash_endpoint.path.rstrip("/") + url.path,
            )

        playback_urls.append({
            "assetId": asset_id,
            "packagingConfigurationId": packaging_configuration_id,
            "vodPlaybackUrl": urlunparse(url),
            "adOffsets": asset_tags.get("AdOffsets"),
        })

    logger.info("Generated playback URLs: %s", json.dumps(playback_urls, default=str))
    return playback_urls


def get_object_arn_from_url(url: str) -> str:
    """Convert an S3 HTTPS URL to an S3 object ARN."""
    parsed = urlparse(url)
    bucket = parsed.netloc
    key = parsed.path
    return f"arn:aws:s3:::{bucket}{key}"


def create_resource_id_from_mediaconvert_job_output(job_output: str) -> str:
    """Create a MediaPackage-compliant resource ID from a MediaConvert job output path."""
    parsed = urlparse(job_output)
    # Remove non-alphanumeric characters except hyphens
    pattern = r"[^a-zA-Z0-9-]"
    asset_id = re.sub(pattern, "", os.path.splitext(parsed.path.lstrip("/"))[0])
    return asset_id


def update_asset(asset: dict[str, Any]) -> dict[str, Any]:
    """Delete and recreate an existing MediaPackage VOD asset."""
    try:
        mediapackage.delete_asset(Id=asset["Id"])
        logger.info("Deleted existing asset: %s", asset["Id"])
        sleep(10)
        new_asset = mediapackage.create_asset(**asset)
        logger.info("Recreated asset: %s", json.dumps({"Id": new_asset["Id"]}))
        return new_asset
    except mediapackage.exceptions.NotFoundException as error:
        raise ValueError(error.response["Error"]["Message"]) from error


def lambda_handler(event: dict[str, Any], context: Any) -> str:
    """
    Lambda handler for MediaPackage VOD asset creation.
    
    Triggered by MediaConvert COMPLETE status events via EventBridge.
    Creates MediaPackage VOD assets and emits playback URL events.
    """
    logger.debug("Received event: %s", json.dumps(event))

    asset_tags = {
        "stack-id": os.environ.get("StackId", ""),
        "stack-name": os.environ.get("StackName", ""),
    }

    if event["detail"].get("userMetadata"):
        asset_tags.update(event["detail"]["userMetadata"])

    try:
        output_details = event["detail"]["outputGroupDetails"]
        if not output_details or not output_details[0].get("playlistFilePaths"):
            logger.warning("No playlist file paths in event")
            return json.dumps({"status": "NO_OUTPUT"})

        job_output = output_details[0]["playlistFilePaths"][0]
        
        asset = {
            "PackagingGroupId": os.environ["MediaPackagePackagingGroupId"],
            "Id": create_resource_id_from_mediaconvert_job_output(job_output),
            "SourceArn": get_object_arn_from_url(job_output),
            "SourceRoleArn": os.environ["MediaPackageReadS3RoleArn"],
            "Tags": asset_tags,
        }
        logger.info("Creating MediaPackage asset: %s", json.dumps(asset))

        # Randomize delay to minimize asset creation collisions
        sleep(randint(10, 30) / 10)

        asset_response = mediapackage.create_asset(**asset)

    except mediapackage.exceptions.UnprocessableEntityException as error:
        error_message = error.response["Error"]["Message"]
        if "exists" in error_message:
            logger.info("Asset already exists, recreating: %s", error_message)
            asset_response = update_asset(asset)
        else:
            raise

    # Emit playback URL event
    playback_url_event = events.put_events(
        Entries=[
            {
                "Detail": json.dumps(
                    {
                        "playbackUrls": generate_playback_urls(
                            asset_response["EgressEndpoints"],
                            asset_response["Id"],
                            asset_tags,
                        )
                    },
                    default=str,
                ),
                "DetailType": "Playback URLs",
                "Source": os.environ.get("StackName", "fast-channels"),
            }
        ]
    )
    logger.info("Emitted playback URL event: %s", json.dumps(playback_url_event, default=str))

    response = mediapackage.describe_asset(Id=asset_response["Id"])
    return json.dumps(response, default=str)
