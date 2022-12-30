from __future__ import print_function
from crhelper import CfnResource
import logging
import boto3
import json
import os
import time
import random

#set debug level if LogLevel environment variable exists, else set to INFO
boto_level = os.environ['LogLevel'].upper() if 'LogLevel' in os.environ else 'INFO'
log_level = os.environ['LogLevel'].upper() if 'LogLevel' in os.environ else 'INFO'

logger = logging.getLogger(__name__)
# Initialise the helper, all inputs are optional, this example shows the defaults
helper = CfnResource(json_logging=False, log_level=log_level, boto_level=boto_level, sleep_on_delete=120, ssl_verify=None)

logger.info(boto3.__version__)

try:
    endpoint = boto3.client('mediaconvert').describe_endpoints()['Endpoints'][0]['Url']
    logger.info('Endpoint URL: %s', endpoint)
    mediaconvert = boto3.client('mediaconvert', endpoint_url=endpoint)
    mediapackage = boto3.client('mediapackage-vod')
    s3 = boto3.resource('s3')
    pass
except Exception as e:
    helper.init_failure(e)

def generate_settings_from_preset(preset):
    preset = mediaconvert.get_preset(Name=preset)['Preset']['Settings']
    logger.info('Preset Settings: %s', json.dumps(preset))
    if 'VideoDescription' in preset:
        if preset['VideoDescription']['CodecSettings']['Codec'] == 'H_265':
            preset['VideoDescription']['CodecSettings']['H265Settings']['FramerateControl'] = 'SPECIFIED'
            preset['VideoDescription']['CodecSettings']['H265Settings']['FramerateNumerator'] = 30
            preset['VideoDescription']['CodecSettings']['H265Settings']['FramerateDenominator'] = 1
        elif preset['VideoDescription']['CodecSettings']['Codec'] == 'H_264':
            preset['VideoDescription']['CodecSettings']['H264Settings']['FramerateControl'] = 'SPECIFIED'
            preset['VideoDescription']['CodecSettings']['H264Settings']['FramerateNumerator'] = 30
            preset['VideoDescription']['CodecSettings']['H264Settings']['FramerateDenominator'] = 1
        else:
            raise ValueError('Unknown Codec. Must be either H_264 or H_265')
    return preset

def format_template_for_slate(template, input, SlateName, MediaConvertTranscodeRoleArn):

    try:
        template['Role'] = MediaConvertTranscodeRoleArn
        template['Settings']['Inputs'][0] = {
            'VideoGenerator': {
                'Duration': input
            },
            "AudioSelectors": {
            "Audio Selector 1": {
                "DefaultSelection": "DEFAULT"
            }
            }
        }
        template['Settings']['OutputGroups'][0]['OutputGroupSettings']['HlsGroupSettings']['Destination'] += SlateName
        template['Settings']['OutputGroups'][0]['OutputGroupSettings']['HlsGroupSettings']['SegmentLength'] = 2
        for Output in template['Settings']['OutputGroups'][0]['Outputs']:
            logger.info('Output: %s', json.dumps(Output))
            Output.update(generate_settings_from_preset(Output['Preset']))
        del template['Description'],template['Category'],template['Name'],template['Arn'],template['CreatedAt'],template['LastUpdated'],template['Type']
    except Exception as error:
        raise error
    logger.info('Job JSON: %s', json.dumps(template))
    return template

@helper.create
@helper.update
def create(event, context):
    logger.info("Got Create")
    # Optionally return an ID that will be used for the resource PhysicalResourceId, 
    # if None is returned an ID will be generated. If a poll_create function is defined 
    # return value is placed into the poll event as event['CrHelperData']['PhysicalResourceId']
    #
    # To add response data update the helper.Data dict
    # If poll is enabled data is placed into poll event as event['CrHelperData']

    if 'Name' in event['ResourceProperties']:
        PhysicalResourceId = event['ResourceProperties']['Name']
    else:
        PhysicalResourceId = 'AdBreakSlate_'+str(event['ResourceProperties']['SlateDurationInMillis'])

    try:
        SlateDurationInMillis = int(event['ResourceProperties']['SlateDurationInMillis'])
        MediaConvertTranscodeRoleArn = event['ResourceProperties']['MediaConvertTranscodeRoleArn']
        template = mediaconvert.get_job_template(Name=event['ResourceProperties']['MediaConvertJobTemplate']['Name'])['JobTemplate']
        job = mediaconvert.create_job(**format_template_for_slate(template, SlateDurationInMillis, PhysicalResourceId, MediaConvertTranscodeRoleArn))['Job']
        result = {'Status': job['Status'],'Id': job['Id']}
        logger.info('Result: %s', json.dumps(result))
        while result['Status'] == 'SUBMITTED' or result['Status'] == 'PROGRESSING':
            time.sleep(5)
            result['Status'] = mediaconvert.get_job(Id=result['Id'])['Job']['Status']
            logger.info('Result: %s', result)
        if result['Status'] == 'ERROR':
            raise ValueError('MediaConvert {} Job failed. Check MediaConvert console.', format(result['Id']))   
    except Exception as error:
        raise error

    return PhysicalResourceId


@helper.delete
def delete(event, context):
    logger.info("Got Delete")
    # Delete never returns anything. Should not fail if the underlying resources are already deleted.
    # Desired state.
    if 'MediaPackagePackagingGroup' in event['ResourceProperties']:
        try:
            Assets = mediapackage.list_assets(PackagingGroupId=event['ResourceProperties']['MediaPackagePackagingGroup']['Id'],MaxResults=100)['Assets']
            for Asset in Assets:
                if 'AdBreakSlate_' in Asset['Id']:
                    try: 
                        mediapackage.delete_asset(Id=Asset['Id'])
                        logger.info('Deleted Asset: %s', Asset['Id'])
                    except Exception:
                        raise Exception
        except mediapackage.exceptions.NotFoundException as error:
            logger.info(error)
        except Exception:
            raise Exception

    try:
        bucket = s3.Bucket(event['ResourceProperties']['VideoDestinationBucket'])
        bucket.objects.filter(Prefix=event['PhysicalResourceId']).delete()
    except Exception:
        raise Exception

def lambda_handler(event, context):
    logger.info('Event: %s', json.dumps(event))
    time.sleep(random.randint(1, 10)) #sleep for a random time between 1-10s to stagger completion of adbreak slate resources.
    helper(event, context)