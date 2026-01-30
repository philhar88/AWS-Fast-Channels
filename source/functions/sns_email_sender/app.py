"""
SNS Email Sender Lambda Function

Sends playback URL notifications via SNS email.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import boto3
import yaml
from botocore.config import Config

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS client
config = Config(retries={"max_attempts": 3, "mode": "adaptive"})
sns_client = boto3.client("sns", config=config)


def dict_to_yaml(data: dict[str, Any]) -> str:
    """Convert a dictionary to YAML format for readable email content."""
    return yaml.dump(data, default_flow_style=False, sort_keys=False)


def publish_to_sns(message: str, subject: str) -> dict[str, Any]:
    """Publish a message to the configured SNS topic."""
    topic_arn = os.environ["SnsTopicArn"]
    
    response = sns_client.publish(
        TopicArn=topic_arn,
        Subject=subject,
        Message=message,
    )
    
    logger.info("Published to SNS, MessageId: %s", response.get("MessageId"))
    return response


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda handler for SNS email notifications.
    
    Triggered by Playback URLs events via EventBridge.
    Converts playback URL data to YAML and sends via SNS email.
    """
    logger.debug("Received event: %s", event)

    payload = event.get("detail", {})
    logger.debug("Payload: %s", payload)

    yaml_payload = dict_to_yaml(payload)
    stack_name = os.environ.get("StackName", "FAST-Channels")
    subject = f"[{stack_name}] New playback URLs available"

    return publish_to_sns(yaml_payload, subject)
