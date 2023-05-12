from __future__ import print_function
from crhelper import CfnResource
import logging
import boto3
import json
import os

#set debug level if LogLevel environment variable exists, else set to INFO
boto_level = os.environ['LogLevel'].upper() if 'LogLevel' in os.environ else 'INFO'
log_level = os.environ['LogLevel'].upper() if 'LogLevel' in os.environ else 'INFO'

logger = logging.getLogger(__name__)
# Initialise the helper, all inputs are optional, this example shows the defaults
helper = CfnResource(json_logging=False, log_level=log_level, boto_level=boto_level, sleep_on_delete=120, ssl_verify=None)

#Initialise boto3
mediatailor = boto3.client('mediatailor')
mediapackage = boto3.client('mediapackage-vod')

try:
    pass
except Exception as e:
    helper.init_failure(e)

def get_channel_outputs_from_mediapackage(PackagingGroupId):
    Outputs = []
    try:
        response = mediapackage.list_packaging_configurations(
            PackagingGroupId=PackagingGroupId
        )
        for PackagingConfiguration in response['PackagingConfigurations']:
            logger.info(PackagingConfiguration)
            if "HlsPackage" in PackagingConfiguration:
                output = {
                        'HlsPlaylistSettings': {
                            'ManifestWindowSeconds': 60
                        },
                        'ManifestName': PackagingConfiguration['Id'],
                        'SourceGroup': PackagingConfiguration['Id']
                    }
                Outputs.append(output)
            if "CmafPackage" in PackagingConfiguration:
                output = {
                        'HlsPlaylistSettings': {
                            'ManifestWindowSeconds': 60
                        },
                        'ManifestName': PackagingConfiguration['Id'],
                        'SourceGroup': PackagingConfiguration['Id']
                    }
                Outputs.append(output)
            if "DashPackage" in PackagingConfiguration:
                output = {
                        'DashPlaylistSettings': {
                            'ManifestWindowSeconds': 60,
                            'MinBufferTimeSeconds': 30,
                            'MinUpdatePeriodSeconds': 2,
                            'SuggestedPresentationDelaySeconds': 10
                        },
                        'ManifestName': PackagingConfiguration['Id'],
                        'SourceGroup': PackagingConfiguration['Id']
                    }
                Outputs.append(output)
        logger.info('Outputs %s:', Outputs)
        return Outputs
        
    except Exception as error:
        raise ValueError(error)
        
@helper.create
def create(event, context):
    logger.info("Got Create")
    # Optionally return an ID that will be used for the resource PhysicalResourceId, 
    # if None is returned an ID will be generated. If a poll_create function is defined 
    # return value is placed into the poll event as event['CrHelperData']['PhysicalResourceId']
    #
    # To add response data update the helper.Data dict
    # If poll is enabled data is placed into poll event as event['CrHelperData']
    PackagingGroupId = event['ResourceProperties']['MediaPackagePackagingGroup']['Id']

    try:
        outputs = get_channel_outputs_from_mediapackage(event['ResourceProperties']['MediaPackagePackagingGroup']['Id'])
        channel = mediatailor.create_channel(
            ChannelName=event['PhysicalResourceId'],
            Outputs=outputs,
            PlaybackMode='LOOP',
            Tags={
                'stack-id':event['StackId'],
                'stack-name':event['ResourceProperties']['StackName'],
            }
        )
        logger.info('Created Channel: %s', channel['ChannelName'])
        policy = {
            'Version': '2012-10-17', 
            'Statement': 
                [
                    {
                        'Sid': 'AllowAnonymous', 
                        'Effect': 'Allow', 
                        'Principal': '*', 
                        'Action': 'mediatailor:GetManifest', 
                        'Resource': channel['Arn']
                    }
                ]
        }
        logger.info('Channel Policy: %s', policy)
        mediatailor.put_channel_policy(
            ChannelName=event['PhysicalResourceId'],
            Policy=json.dumps(policy)
        )
        PlaybackBaseUrl = channel['Outputs'][0]['PlaybackUrl'].rsplit("/",1)[0]
        helper.Data['PlaybackBaseUrl'] = PlaybackBaseUrl
    except Exception as error:
        raise ValueError(error)

    return event['PhysicalResourceId']


@helper.update
def update(event, context):
    logger.info("Got Update")
    # If the update resulted in a new resource being created, return an id for the new resource. 
    # CloudFormation will send a delete event with the old id when stack update completes
    PackagingGroupId = event['ResourceProperties']['MediaPackagePackagingGroup']['Id']
    event['PhysicalResourceId']
    try:
        outputs = get_channel_outputs_from_mediapackage(event['ResourceProperties']['MediaPackagePackagingGroup']['Id'])
        channel = mediatailor.update_channel(
            ChannelName=event['PhysicalResourceId'],
            Outputs=outputs,
        )
        PlaybackBaseUrl = channel['Outputs'][0]['PlaybackUrl'].rsplit("/",1)[0] + '/'
        helper.Data['PlaybackBaseUrl'] = PlaybackBaseUrl
    except Exception as error:
        raise ValueError(error)    

@helper.delete
def delete(event, context):
    logger.info("Got Delete")
    # Delete never returns anything. Should not fail if the underlying resources are already deleted.
    # Desired state.
    PackagingGroupId = event['ResourceProperties']['MediaPackagePackagingGroup']['Id']
    event['PhysicalResourceId']
    try:
        channel = mediatailor.delete_channel(ChannelName=event['PhysicalResourceId'])
    except mediatailor.exceptions.BadRequestException as error:
        raise ValueError(error.response['Error']['Message'])
    except Exception as error:
        raise ValueError(error)

def lambda_handler(event, context):
    try:
        event['PhysicalResourceId'] = event['ResourceProperties']['Name']
    except:
        logger.info('No Name property detected')
    helper(event, context)