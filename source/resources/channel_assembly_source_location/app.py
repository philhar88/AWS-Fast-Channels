from __future__ import print_function
from crhelper import CfnResource
import logging
import os
import botocore
import boto3
import json

#set debug level if LogLevel environment variable exists, else set to INFO
boto_level = os.environ['LogLevel'].upper() if 'LogLevel' in os.environ else 'INFO'
log_level = os.environ['LogLevel'].upper() if 'LogLevel' in os.environ else 'INFO'

logger = logging.getLogger(__name__)
# Initialise the helper, all inputs are optional, this example shows the defaults
helper = CfnResource(json_logging=False, log_level=log_level, boto_level=boto_level, sleep_on_delete=120, ssl_verify=None)

#Initialise boto3
client = boto3.client('mediatailor')

try:
    pass
except Exception as e:
    helper.init_failure(e)


@helper.create
def create(event, context):
    logger.info("Got Create")
    # Optionally return an ID that will be used for the resource PhysicalResourceId, 
    # if None is returned an ID will be generated. If a poll_create function is defined 
    # return value is placed into the poll event as event['CrHelperData']['PhysicalResourceId']
    #
    # To add response data update the helper.Data dict
    # If poll is enabled data is placed into poll event as event['CrHelperData']
    
    if event['ResourceProperties']['Name']:
        PhysicalResourceId = event['ResourceProperties']['Name']
    else:
        PhysicalResourceId = helper.generate_physical_id(event)

    try:
        create_source_location_request = {
            'AccessConfiguration': {
                'AccessType':'SECRETS_MANAGER_ACCESS_TOKEN',
                'SecretsManagerAccessTokenConfiguration':{
                    'HeaderName':'X-MediaPackage-CDNIdentifier',
                    'SecretArn':event['ResourceProperties']['MediaPackageAccessSecretArn'],
                    'SecretStringKey':'MediaPackageCDNIdentifier'
                }
            },
            'DefaultSegmentDeliveryConfiguration':{
                'BaseUrl': 'https://' + event['ResourceProperties']['CloudFrontDistribution']['DomainName']
            },
            'HttpConfiguration':{
                'BaseUrl': event['ResourceProperties']['MediaPackagePackagingGroup']['DomainName']
            },
            'SourceLocationName': PhysicalResourceId,
            'Tags': {
                'stack-id':event['StackId'],
                'stack-name':event['ResourceProperties']['StackName'],
            }
        }
        logger.info('SourceLocation: %s', create_source_location_request)
        response = client.create_source_location(**create_source_location_request)
    except client.exceptions.BadRequestException as error:
        if "exists" in error.response['Error']['Message']:
            logger.info('SourceLocation: %s already exists', PhysicalResourceId)
        else:
            logger.info('Error: %s', error)
            raise ValueError(error) 
    
    return PhysicalResourceId


@helper.update
def update(event, context):
    logger.info("Got Update")
    # If the update resulted in a new resource being created, return an id for the new resource. 
    # CloudFormation will send a delete event with the old id when stack update completes

    try:
        create_source_location_request = {
            'AccessConfiguration': {
                'AccessType':'SECRETS_MANAGER_ACCESS_TOKEN',
                'SecretsManagerAccessTokenConfiguration':{
                    'HeaderName':'X-MediaPackage-CDNIdentifier',
                    'SecretArn':event['ResourceProperties']['MediaPackageSecretArn'],
                    'SecretStringKey':'MediaPackageCDNIdentifier'
                }
            },
            'DefaultSegmentDeliveryConfiguration':{
                'BaseUrl': 'https://' + event['ResourceProperties']['CloudFrontDistribution']['DomainName']
            },
            'HttpConfiguration':{
                'BaseUrl': event['ResourceProperties']['MediaPackagePackagingGroup']['DomainName']
            },
            'SourceLocationName': event['PhysicalResourceId']
        }
        response = client.update_source_location(**create_source_location_request)
    except botocore.exceptions.ClientError as error:
        if error.response['Error']['Code'] == 'NotFoundException':
            response = client.create_source_location(**create_source_location_request)
        else:
            raise ValueError(error)
    except error:
        raise ValueError(error)

@helper.delete
def delete(event, context):
    logger.info("Got Delete")
    # Delete never returns anything. Should not fail if the underlying resources are already deleted.
    # Desired state.
    try:
        VodSources = client.list_vod_sources(SourceLocationName=event['PhysicalResourceId'],MaxResults=100)['Items']
        for VodSource in VodSources:
            if 'AdBreakSlate_' in VodSource['VodSourceName']:
                try: 
                    client.delete_vod_source(SourceLocationName=event['PhysicalResourceId'], VodSourceName=VodSource['VodSourceName'])
                    logger.info('Removed Vod Source: %s', VodSource['VodSourceName'])
                except Exception as error:
                    raise ValueError('Unable to delete AdBreakSlate Vod Sources. Please manually delete from SourceLocation and try again. %s', error)
        client.delete_source_location(SourceLocationName=event['PhysicalResourceId'])
    except client.exceptions.BadRequestException as error:
        if "referring" in error.response['Error']['Message']:
            raise ValueError('SourceLocation must not contain any Vod or Live Sources. Please delete all Sources and try again.')
        else:
            raise ValueError(error.response['Error']['Message'])
    except Exception as error:
        raise ValueError(error)

def lambda_handler(event, context):
    logger.info('Event: %s', event)
    helper(event, context)