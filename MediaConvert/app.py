import boto3
import logging
import json
import os

#set debug level if LogLevel environment variable exists, else set to INFO
boto_level = os.environ['LogLevel'].upper() if 'LogLevel' in os.environ else 'INFO'
log_level = os.environ['LogLevel'].upper() if 'LogLevel' in os.environ else 'INFO'

logger = logging.getLogger(__name__)

try:
    endpoint = boto3.client('mediaconvert').describe_endpoints()['Endpoints'][0]['Url']
    logger.info('Endpoint URL: %s', endpoint)
    mediaconvert = boto3.client('mediaconvert', endpoint_url=endpoint)
except Exception as error:
    raise error

def format_template_for_new_job(template, input):
    try:
        template['JobTemplate'] = template['Name']
        template['Role'] = os.environ['MediaConvertTranscodeRoleArn']
        template['Settings']['Inputs'][0]['FileInput'] = input
        del template['Description'],template['Category'],template['Name'],template['Arn'],template['CreatedAt'],template['LastUpdated'],template['Type']
    except Exception as error:
        raise error
    logger.info('MediaConvert Job JSON: %s', json.dumps(template))
    return template

def lambda_handler(event, context):
    logger.info('Event: %s', json.dumps(event))
    try:   
        template = mediaconvert.get_job_template(Name=os.environ['MediaConvertJobTemplate'])['JobTemplate']
        input = 's3://' + event['detail']['bucket']['name'] + '/' + event['detail']['object']['key']
        job = mediaconvert.create_job(**format_template_for_new_job(template, input))['Job']
        result = {'Status': job['Status'],'Id': job['Id']}
        logger.info('Result: %s', json.dumps(result))
    except Exception as error:
        raise error

    return json.dumps(result)