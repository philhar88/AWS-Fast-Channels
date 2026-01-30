"""
MediaTailor Channel Assembly Channel Custom Resource

CloudFormation custom resource for creating and managing MediaTailor channels.
"""
from __future__ import annotations

import json
import logging
import os
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
mediatailor = boto3.client("mediatailor", config=config)
mediapackage = boto3.client("mediapackage-vod", config=config)


def get_channel_outputs_from_mediapackage(packaging_group_id: str) -> list[dict[str, Any]]:
    """Generate channel outputs from MediaPackage packaging configurations."""
    outputs = []
    
    response = mediapackage.list_packaging_configurations(
        PackagingGroupId=packaging_group_id
    )
    
    for config in response.get("PackagingConfigurations", []):
        logger.info("Processing packaging configuration: %s", config.get("Id"))
        
        output_base = {
            "ManifestName": config["Id"],
            "SourceGroup": config["Id"],
        }
        
        if "HlsPackage" in config or "CmafPackage" in config:
            output = {
                **output_base,
                "HlsPlaylistSettings": {"ManifestWindowSeconds": 60},
            }
            outputs.append(output)
        
        if "DashPackage" in config:
            output = {
                **output_base,
                "DashPlaylistSettings": {
                    "ManifestWindowSeconds": 60,
                    "MinBufferTimeSeconds": 30,
                    "MinUpdatePeriodSeconds": 2,
                    "SuggestedPresentationDelaySeconds": 10,
                },
            }
            outputs.append(output)
    
    logger.info("Generated %d channel outputs", len(outputs))
    return outputs


@helper.create
def create(event: dict[str, Any], context: Any) -> str:
    """Handle CloudFormation Create event."""
    logger.info("Processing Create request")
    
    properties = event["ResourceProperties"]
    packaging_group_id = properties["MediaPackagePackagingGroup"]["Id"]
    physical_resource_id = event.get("PhysicalResourceId") or properties.get("Name")

    outputs = get_channel_outputs_from_mediapackage(packaging_group_id)
    
    channel = mediatailor.create_channel(
        ChannelName=physical_resource_id,
        Outputs=outputs,
        PlaybackMode="LOOP",
        Tags={
            "stack-id": event["StackId"],
            "stack-name": properties["StackName"],
        },
    )
    logger.info("Created channel: %s", channel["ChannelName"])

    # Create channel policy allowing anonymous manifest access
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowAnonymous",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "mediatailor:GetManifest",
                "Resource": channel["Arn"],
            }
        ],
    }
    logger.info("Applying channel policy")
    mediatailor.put_channel_policy(
        ChannelName=physical_resource_id,
        Policy=json.dumps(policy),
    )

    # Extract playback base URL for CloudFormation outputs
    playback_base_url = channel["Outputs"][0]["PlaybackUrl"].rsplit("/", 1)[0]
    helper.Data["PlaybackBaseUrl"] = playback_base_url

    return physical_resource_id


@helper.update
def update(event: dict[str, Any], context: Any) -> str:
    """Handle CloudFormation Update event."""
    logger.info("Processing Update request")
    
    properties = event["ResourceProperties"]
    packaging_group_id = properties["MediaPackagePackagingGroup"]["Id"]
    physical_resource_id = event["PhysicalResourceId"]

    outputs = get_channel_outputs_from_mediapackage(packaging_group_id)
    
    channel = mediatailor.update_channel(
        ChannelName=physical_resource_id,
        Outputs=outputs,
    )
    
    playback_base_url = channel["Outputs"][0]["PlaybackUrl"].rsplit("/", 1)[0] + "/"
    helper.Data["PlaybackBaseUrl"] = playback_base_url

    return physical_resource_id


@helper.delete
def delete(event: dict[str, Any], context: Any) -> None:
    """Handle CloudFormation Delete event."""
    logger.info("Processing Delete request")
    
    physical_resource_id = event["PhysicalResourceId"]

    try:
        # Stop the channel first if running
        try:
            mediatailor.stop_channel(ChannelName=physical_resource_id)
            logger.info("Stopped channel: %s", physical_resource_id)
        except mediatailor.exceptions.BadRequestException:
            pass  # Channel might already be stopped or not exist

        mediatailor.delete_channel(ChannelName=physical_resource_id)
        logger.info("Deleted channel: %s", physical_resource_id)

    except mediatailor.exceptions.BadRequestException as error:
        error_message = error.response["Error"]["Message"]
        if "not found" in error_message.lower():
            logger.info("Channel already deleted: %s", physical_resource_id)
        else:
            raise ValueError(error_message) from error
    except Exception as error:
        raise ValueError(str(error)) from error


def lambda_handler(event: dict[str, Any], context: Any) -> None:
    """Lambda entry point for CloudFormation custom resource."""
    if "PhysicalResourceId" not in event:
        event["PhysicalResourceId"] = event["ResourceProperties"].get("Name")
    
    logger.info("Received event: %s", json.dumps(event, default=str))
    helper(event, context)
