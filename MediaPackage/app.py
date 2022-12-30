import os
import boto3
import re
from urllib.parse import urlparse, urlunparse
import time
import json
import logging

logger = logging.getLogger()

def init_logging():
    try:
        logger.setLevel(logging.os.environ['LogLevel'].upper())
    except:
        logger.setLevel(logging.INFO)

init_logging()

try:
    mediapackage = boto3.client('mediapackage-vod')
    s3 = boto3.client("s3")
    events = boto3.client("events")
except Exception as error:
    raise error

StackName = os.environ['StackName']

def generate_playback_urls(EgressEndpoints, AssetId):

    PlaybackUrls = {}
    MediaTailorPlaybackEndpoints = {
        'Hls':urlparse(os.environ['MediaTailorPlaybackConfigurationVodHls']),
        'Dash':urlparse(os.environ['MediaTailorPlaybackConfigurationVodDash'])
    }
    for EgressEndpoint in EgressEndpoints:
        Url = urlparse(EgressEndpoint['Url'])
        PackagingConfigurationId = EgressEndpoint['PackagingConfigurationId']
        if os.path.splitext(urlparse(EgressEndpoint['Url']).path)[1] == '.m3u8':
            Url = Url._replace(netloc=MediaTailorPlaybackEndpoints['Hls'].netloc, path=MediaTailorPlaybackEndpoints['Hls'].path.rstrip("/")+Url.path)
            Url = urlunparse(Url)
        if os.path.splitext(urlparse(EgressEndpoint['Url']).path)[1] == '.mpd':
            Url = Url._replace(netloc=MediaTailorPlaybackEndpoints['Dash'].netloc, path=MediaTailorPlaybackEndpoints['Dash'].path.rstrip("/")+Url.path)
            Url = urlunparse(Url)
        PlaybackUrls.update({StackName+':'+AssetId+':'+PackagingConfigurationId:Url})
    logger.info('PlaybackUrls: %s', json.dumps(PlaybackUrls))
    return PlaybackUrls

def get_object_arn_from_url(url):
    try:
        url = urlparse(url)
        bucket = url.netloc
        key = url.path
        arn = f'arn:aws:s3:::{bucket+key}'
        return arn
    except Exception as error:
        return error

def create_resource_id_from_mediaconvert_job_output(job_output):
    job_output = urlparse(job_output)
    pattern = r'[^a-zA-Z0-9-]'
    try:
        Id = os.path.splitext(job_output.path.lstrip("/"))[0]
        return Id
    except Exception as error:
        raise error
        return None

def lambda_handler(event, context):
    logger.info('Event: %s', json.dumps(event))
    job_output = event['detail']['outputGroupDetails'][0]['playlistFilePaths'][0]
    try:
        asset = {
            'PackagingGroupId':os.environ['MediaPackagePackagingGroupId'],
            'Id':create_resource_id_from_mediaconvert_job_output(job_output),
            'SourceArn':get_object_arn_from_url(job_output),
            'SourceRoleArn':os.environ['MediaPackageReadS3RoleArn'],
        }
        logger.info('MediaPackage Asset JSON: %s', json.dumps(asset))
        try:
            asset = mediapackage.create_asset(**asset)
        except mediapackage.exceptions.UnprocessableEntityException as error:
            if "exists" in error.response['Error']['Message']:
                logger.info(error.response['Error']['Message'])
            else:
                raise error
            mediapackage.delete_asset(Id=asset['Id'])
            time.sleep(5)
            try:
                asset = mediapackage.create_asset(**asset)
            except:
                raise
        Tag = mediapackage.tag_resource(
            ResourceArn=asset['Arn'],
            Tags={
                'stack-id':os.environ['StackId'],
                'stack-name':os.environ['StackName'],
            }
        )
        EventBridgeEvent = events.put_events(
            Entries=[
                {
                    'Detail':json.dumps(generate_playback_urls(asset['EgressEndpoints'],asset['Id']), indent=1),
                    'DetailType':'Playback URLs',
                    'Source':StackName
                }
            ]
        )
        logger.info(EventBridgeEvent)
    except Exception as error:
        raise error

    return mediapackage.describe_asset(Id=asset['Id'])