"""
MediaTailor Source Location Custom Resource

CloudFormation custom resource for creating and managing MediaTailor source locations.
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

# Initialize AWS client
config = Config(retries={"max_attempts": 3, "mode": "adaptive"})
client = boto3.client("mediatailor", config=config)


def build_source_location_config(
    event: dict[str, Any],
    physical_resource_id: str,
    include_tags: bool = True,
) -> dict[str, Any]:
    """Build the source location configuration from event properties."""
    properties = event["ResourceProperties"]
    
    config = {
        "AccessConfiguration": {
            "AccessType": "SECRETS_MANAGER_ACCESS_TOKEN",
            "SecretsManagerAccessTokenConfiguration": {
                "HeaderName": "X-MediaPackage-CDNIdentifier",
                "SecretArn": properties["MediaPackageAccessSecretArn"],
                "SecretStringKey": "MediaPackageCDNIdentifier",
            },
        },
        "DefaultSegmentDeliveryConfiguration": {
            "BaseUrl": f"https://{properties['CloudFrontDistribution']['DomainName']}"
        },
        "HttpConfiguration": {
            "BaseUrl": properties["MediaPackagePackagingGroup"]["DomainName"]
        },
        "SourceLocationName": physical_resource_id,
    }
    
    if include_tags:
        config["Tags"] = {
            "stack-id": event["StackId"],
            "stack-name": properties["StackName"],
        }
    
    return config


@helper.create
def create(event: dict[str, Any], context: Any) -> str:
    """Handle CloudFormation Create event."""
    logger.info("Processing Create request")
    
    properties = event["ResourceProperties"]
    physical_resource_id = properties.get("Name") or helper.generate_physical_id(event)

    source_location_config = build_source_location_config(event, physical_resource_id)
    
    logger.info("Creating source location: %s", json.dumps(source_location_config, default=str))
    
    try:
        client.create_source_location(**source_location_config)
        logger.info("Created source location: %s", physical_resource_id)
    except client.exceptions.BadRequestException as error:
        if "exists" in error.response["Error"]["Message"]:
            logger.info("Source location already exists: %s", physical_resource_id)
        else:
            raise ValueError(error.response["Error"]["Message"]) from error

    return physical_resource_id


@helper.update
def update(event: dict[str, Any], context: Any) -> str:
    """Handle CloudFormation Update event."""
    logger.info("Processing Update request")
    
    physical_resource_id = event["PhysicalResourceId"]
    source_location_config = build_source_location_config(
        event, physical_resource_id, include_tags=False
    )

    try:
        client.update_source_location(**source_location_config)
        logger.info("Updated source location: %s", physical_resource_id)
    except client.exceptions.NotFoundException:
        client.create_source_location(**source_location_config)
        logger.info("Source location not found, created: %s", physical_resource_id)

    return physical_resource_id


@helper.delete
def delete(event: dict[str, Any], context: Any) -> None:
    """Handle CloudFormation Delete event."""
    logger.info("Processing Delete request")
    
    physical_resource_id = event["PhysicalResourceId"]

    try:
        # Try to delete VOD sources first, but don't fail if we can't list them
        # (permissions might be gone during rollback)
        try:
            vod_sources = client.list_vod_sources(
                SourceLocationName=physical_resource_id,
                MaxResults=100,
            )
            
            for vod_source in vod_sources.get("Items", []):
                vod_source_name = vod_source["VodSourceName"]
                if "AdBreakSlate_" in vod_source_name:
                    try:
                        client.delete_vod_source(
                            SourceLocationName=physical_resource_id,
                            VodSourceName=vod_source_name,
                        )
                        logger.info("Deleted VOD source: %s", vod_source_name)
                    except Exception as error:
                        logger.warning("Failed to delete VOD source %s: %s", vod_source_name, error)
        except Exception as list_error:
            # If we can't list VOD sources (e.g., permission denied during rollback),
            # continue with source location deletion anyway
            logger.warning("Could not list VOD sources (may be expected during rollback): %s", list_error)

        client.delete_source_location(SourceLocationName=physical_resource_id)
        logger.info("Deleted source location: %s", physical_resource_id)

    except client.exceptions.BadRequestException as error:
        error_message = error.response["Error"]["Message"]
        if "referring" in error_message:
            # Source location has VOD sources we couldn't delete - log and succeed
            # to allow CloudFormation to continue cleanup
            logger.warning(
                "Source location has remaining VOD sources. "
                "Manual cleanup may be required: %s", physical_resource_id
            )
        elif "not found" in error_message.lower():
            logger.info("Source location already deleted: %s", physical_resource_id)
        else:
            raise ValueError(error_message) from error
    except Exception as error:
        error_str = str(error).lower()
        if "not found" in error_str or "does not exist" in error_str:
            logger.info("Source location already deleted: %s", physical_resource_id)
        elif "not authorized" in error_str or "accessdenied" in error_str:
            # During rollback, IAM policies may be deleted before custom resources
            # Log warning but don't fail to allow stack cleanup to continue
            logger.warning(
                "Permission denied during delete (may be expected during rollback). "
                "Manual cleanup may be required: %s - Error: %s", 
                physical_resource_id, error
            )
        else:
            raise ValueError(str(error)) from error


def lambda_handler(event: dict[str, Any], context: Any) -> None:
    """Lambda entry point for CloudFormation custom resource."""
    logger.info("Received event: %s", json.dumps(event, default=str))
    helper(event, context)
