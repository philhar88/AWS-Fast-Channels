"""
MediaConvert Job Lambda Function

Triggers a MediaConvert job when a new video file is uploaded to S3.
Supports ad break offsets via S3 object tags for frame-accurate ad insertion.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
import xmltodict
from botocore.config import Config

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize MediaConvert client with regional endpoint
config = Config(retries={"max_attempts": 3, "mode": "adaptive"})
mediaconvert_client = boto3.client("mediaconvert", config=config)
endpoint = mediaconvert_client.describe_endpoints()["Endpoints"][0]["Url"]
logger.info("MediaConvert Endpoint URL: %s", endpoint)
mediaconvert = boto3.client("mediaconvert", endpoint_url=endpoint, config=config)

s3_client = boto3.client("s3", config=config)


def get_s3_object_tags(bucket: str, key: str) -> list[dict[str, str]] | None:
    """Retrieve S3 object tags for the given bucket and key."""
    try:
        response = s3_client.get_object_tagging(Bucket=bucket, Key=key)
        tags = response.get("TagSet", [])
        logger.info("Retrieved tags: %s", json.dumps(tags))
        return tags
    except Exception as error:
        logger.warning("Failed to retrieve tags: %s", error)
        return None


def get_ad_offsets(tags: list[dict[str, str]] | None) -> list[str] | None:
    """Extract ad break offsets from S3 object tags."""
    if not tags:
        return None

    possible_key_names = [
        os.environ.get("AdOffsetS3TagKeyName", "AdOffsets"),
        "ad_offsets",
        "AdOffsets",
        "adoffsets",
        "Adoffsets",
    ]

    for tag in tags:
        if tag.get("Key") in possible_key_names:
            logger.info("Found ad offset key: %s", tag["Key"])
            try:
                ad_offsets = tag["Value"].split()
                logger.info("Parsed ad offsets: %s", ad_offsets)
                return ad_offsets
            except Exception as error:
                logger.warning("Failed to parse ad offsets: %s", error)
                return None

    return None


def generate_esam(ad_offsets: list[str]) -> dict[str, Any]:
    """Generate ESAM (Event Signaling and Management) XML for ad break offsets."""
    response_signals = []
    conditioning_infos = []
    manifests_responses = []

    for index, ad_offset in enumerate(ad_offsets):
        logger.info("Processing ad offset (milliseconds): %s", ad_offset)
        offset_seconds = round(float(ad_offset) / 1000, 3)

        response_signals.append({
            "@acquisitionPointIdentity": "AWSElementalMediaTailor",
            "@acquisitionSignalID": index,
            "@signalPointID": index,
            "@action": "create",
            "sig:NPTPoint": {"@nptPoint": offset_seconds},
            "sig:SCTE35PointDescriptor": {
                "@spliceCommandType": "06",
                "sig:SegmentationDescriptorInfo": {
                    "@segmentEventId": index,
                    "@segmentTypeId": "52",
                },
            },
        })

        conditioning_infos.append({
            "@startOffset": f"PT{offset_seconds}S",
            "@acquisitionSignalIDRef": index,
            "@duration": "PT0S",
            "Segment": "PT0S",
        })

        manifests_responses.append({
            "@acquisitionPointIdentity": "AWSElementalMediaTailor",
            "@acquisitionSignalID": index,
            "@duration": "PT0S",
            "@dataPassThrough": "true",
            "SegmentModify": {
                "FirstSegment": {
                    "Tag": [
                        {"@value": "#EXT-X-CUE-OUT:0"},
                        {"@value": "#EXT-X-CUE-IN"},
                    ]
                }
            },
        })

    scc_xml = {
        "SignalProcessingNotification": {
            "@xmlns": "urn:cablelabs:iptvservices:esam:xsd:signal:1",
            "@xmlns:sig": "urn:cablelabs:md:xsd:signaling:3.0",
            "@xmlns:common": "urn:cablelabs:iptvservices:esam:xsd:common:1",
            "@xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "common:BatchInfo": {
                "@batchId": "abcd",
                "common:Source": {"@xsi:type": "content:MovieType"},
            },
            "ResponseSignal": response_signals,
            "ConditioningInfo": conditioning_infos,
        }
    }

    mcc_xml = {
        "ManifestConfirmConditionNotification": {
            "@xmlns": "http://www.cablelabs.com/namespaces/metadata/xsd/confirmation/2",
            "ManifestResponse": manifests_responses,
        }
    }

    return {
        "ManifestConfirmConditionNotification": {
            "MccXml": xmltodict.unparse(mcc_xml, pretty=False)
        },
        "ResponseSignalPreroll": 0,
        "SignalProcessingNotification": {
            "SccXml": xmltodict.unparse(scc_xml, pretty=False)
        },
    }


def format_template_for_new_job(
    template: dict[str, Any],
    input_file: str,
    esam: dict[str, Any] | None = None,
    ad_offsets: list[str] | None = None,
) -> dict[str, Any]:
    """Format a MediaConvert job template for a new job submission."""
    job_settings = {
        "JobTemplate": template["Name"],
        "Role": os.environ["MediaConvertTranscodeRoleArn"],
        "Settings": template["Settings"].copy(),
    }

    job_settings["Settings"]["Inputs"][0]["FileInput"] = input_file

    if ad_offsets:
        offsets_str = " ".join(ad_offsets)
        job_settings["Tags"] = {"AdOffsets": offsets_str}
        job_settings["UserMetadata"] = {"AdOffsets": offsets_str}

    if esam:
        logger.info("Adding ESAM configuration")
        job_settings["Settings"]["Esam"] = esam

    logger.debug("MediaConvert Job JSON: %s", json.dumps(job_settings))
    return job_settings


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda handler for MediaConvert job creation.
    
    Triggered by S3 Object Created or Object Tagging events via EventBridge.
    """
    logger.debug("Received event: %s", json.dumps(event, default=str))

    input_file_bucket = event["detail"]["bucket"]["name"]
    input_file_key = event["detail"]["object"]["key"]
    
    # Skip processing for non-video files
    video_extensions = {".mp4", ".mov", ".mxf", ".mkv", ".avi", ".ts", ".m2ts"}
    file_ext = os.path.splitext(input_file_key)[1].lower()
    if file_ext not in video_extensions:
        logger.info("Skipping non-video file: %s", input_file_key)
        return {"Status": "SKIPPED", "Reason": "Not a video file"}

    input_file_tags = get_s3_object_tags(input_file_bucket, input_file_key)
    ad_offsets = get_ad_offsets(input_file_tags)
    esam = generate_esam(ad_offsets) if ad_offsets else None

    template = mediaconvert.get_job_template(
        Name=os.environ["MediaConvertJobTemplate"]
    )["JobTemplate"]

    input_file = f"s3://{input_file_bucket}/{input_file_key}"

    job_params = format_template_for_new_job(template, input_file, esam, ad_offsets)
    job = mediaconvert.create_job(**job_params)["Job"]

    result = {
        "Status": job["Status"],
        "Id": job["Id"],
        "InputFile": input_file,
    }
    logger.info("Job created: %s", json.dumps(result))

    return result
