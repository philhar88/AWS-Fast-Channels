import os
import logging
import json
import boto3
import yaml

logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', logging.INFO))

def lambda_handler(event, context):
    '''
    takes in a playbackUrl event from EventBridge
    converts JSON to YAML
    sends to SNS topic
    '''

    logger.debug('Event: %s', event)

    payload = event['detail']

    logger.debug('Payload: %s', payload)

    yaml_payload = dict_to_yaml(payload)

    response = publish_to_sns(yaml_payload)

    return response

def dict_to_yaml(data):
    '''
    converts dict to yaml
    '''
    logger.debug('Data: %s', data)
    yaml_data = yaml.dump(data)
    logger.debug('Yaml Data: %s', yaml_data)
    return yaml_data

def publish_to_sns(message):
    '''
    sends a message to the SNS topic
    '''
    logger.debug('Message: %s', message)
    sns_client = boto3.client('sns')
    topic_arn = os.environ['SnsTopicArn']
    return sns_client.publish(
        TopicArn=topic_arn,
        Subject=f"[{os.environ['StackName']}] New playback urls:",
        Message=message
    )
