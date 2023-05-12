import os
import re
from urllib.parse import urlparse, urlunparse
from random import randint
from time import sleep
import json
import logging
import boto3

# define clients required for script
mediapackage = boto3.client('mediapackage-vod')
s3 = boto3.client("s3")
events = boto3.client("events")
logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', logging.INFO))


def generate_playback_urls(egress_endpoints, asset_id, asset_tags):
    """
    generates the MediaTailor SSAI playback urls dict
    for a given asset_id and its egress_endpoints
    """
    playback_urls = []
    try:
        mediatailor_playback_endpoints = {
            'Hls': urlparse(os.environ['MediaTailorPlaybackConfigurationVodHls']),
            'Dash': urlparse(os.environ['MediaTailorPlaybackConfigurationVodDash'])
        }
    except KeyError:
        logger.info("No MediaTailor environment variables")
        mediatailor_playback_endpoints = {
            'Hls': '',
            'Dash': ''
        }
    try:
        for egress_endpoint in egress_endpoints:
            url = urlparse(egress_endpoint['Url'])
            packaging_configuration_id = egress_endpoint['PackagingConfigurationId']
            if os.path.splitext(urlparse(egress_endpoint['Url']).path)[1] == '.m3u8':
                url = url._replace(netloc=mediatailor_playback_endpoints['Hls'].netloc,
                                   path=mediatailor_playback_endpoints['Hls'].path.rstrip("/")+url.path)
                url = urlunparse(url)
            if os.path.splitext(urlparse(egress_endpoint['Url']).path)[1] == '.mpd':
                url = url._replace(netloc=mediatailor_playback_endpoints['Dash'].netloc,
                                   path=mediatailor_playback_endpoints['Dash'].path.rstrip("/")+url.path)
                url = urlunparse(url)
            playback_urls.append(
                {
                    'assetId': asset_id,
                    'packagingConfigurationId': packaging_configuration_id,
                    'vodPlaybackUrl': url,
                    'adOffsets': asset_tags.get('AdOffsets', None)
                }
            )
        logger.info('playback_urls: %s', json.dumps(
            playback_urls, default=list))
    except Exception as unknown_error:
        playback_urls = None
        raise unknown_error

    return playback_urls


def get_object_arn_from_url(url):
    """returns the s3 object ARN from HTTPS URL"""
    try:
        url = urlparse(url)
        bucket = url.netloc
        key = url.path
        arn = f'arn:aws:s3:::{bucket+key}'
    except Exception as unknown_error:
        arn = None
        raise Exception(unknown_error) from unknown_error

    return arn


def create_resource_id_from_mediaconvert_job_output(job_output):
    """Returns resource id used in MediaPackage as the Asset ID based on MediaConvert job output"""
    job_output = urlparse(job_output)
    pattern = r'[^a-zA-Z0-9-]'
    try:
        asset_id = re.sub(pattern, '', os.path.splitext(
            job_output.path.lstrip("/"))[0])
    except Exception as unknown_error:
        asset_id = None
        raise Exception(unknown_error) from unknown_error
    return asset_id


def update_asset(asset):
    """
    Deletes and re-creates an existing MediaPackage-vod Asset
    """
    try:
        mediapackage.delete_asset(Id=asset['Id'])
        logger.info('Deleted AssetId: %s', asset['Id'])
        sleep(10)
        asset = mediapackage.create_asset(**asset)
        logger.info('Created Asset: %s',
                    json.dumps(
                        {
                            asset['Id']: asset['EgressEndpoints']
                        }
                    )
                    )
    except mediapackage.exceptions.NotFoundException as not_found_exception:
        raise ValueError(
            not_found_exception.response['Error']['Message']) from not_found_exception

    return asset


def lambda_handler(event, context):
    """
    Main function. Takes Job Change Complete event from MediaConvert and
    creates a MediaPackage-vod Asset along with Tags for the current CloudFormation Stack
    """

    logger.debug('Event: %s', json.dumps(event))

    asset_tags = {
        'stack-id': os.environ['StackId'],
        'stack-name': os.environ['StackName'],
    }
    if event['detail'].get('userMetadata', False):
        asset_tags.update(event['detail']['userMetadata'])

    try:
        job_output = event['detail']['outputGroupDetails'][0]['playlistFilePaths'][0]
        asset = {
            'PackagingGroupId': os.environ['MediaPackagePackagingGroupId'],
            'Id': create_resource_id_from_mediaconvert_job_output(job_output),
            'SourceArn': get_object_arn_from_url(job_output),
            'SourceRoleArn': os.environ['MediaPackageReadS3RoleArn'],
            'Tags': asset_tags
        }
        logger.info('MediaPackage Asset JSON: %s', json.dumps(asset))

        # randomize delay to minimize asset create collisions.
        sleep(randint(10, 30)/10)

        asset = mediapackage.create_asset(**asset)

    except mediapackage.exceptions.UnprocessableEntityException as entity_exception:
        error_message = entity_exception.response['Error']['Message']
        if "exists" in error_message:
            # if MediaPackage already has this asset ingested, delete and re-ingest.
            logger.info(error_message)
            asset = update_asset(asset)
        else:
            raise entity_exception

    # Send Event describing MediaTailor Playback URLs for VoD.
    playback_url_event = events.put_events(
        Entries=[
            {
                'Detail': json.dumps(
                    {'playbackUrls': generate_playback_urls(
                        asset['EgressEndpoints'], asset['Id'], asset_tags)}
                , default=str),
                'DetailType':'Playback URLs',
                'Source':os.environ['StackName']
            }
        ]
    )
    logger.info("EventId Sent: %s", json.dumps(
        playback_url_event, default=str))

    response = mediapackage.describe_asset(Id=asset['Id'])

    return json.dumps(response, default=str)
