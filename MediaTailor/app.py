import os
from urllib.parse import urlparse
import json
import logging
from random import randint
from time import sleep
import boto3

#define clients required for script
mediatailor = boto3.client('mediatailor')
events = boto3.client("events")
logger = logging.getLogger()

def init_logging():
    """Initialize logging. Tries to get the log level from env vars but fails over to INFO."""
    try:
        logger.setLevel(logging.os.environ['LogLevel'].upper())
    except KeyError:
        logger.setLevel(logging.INFO)

def parse_arn(arn):
    """http://docs.aws.amazon.com/general/latest/gr/aws-arns-and-namespaces.html"""
    elements = arn.split(':', 5)
    result = {
        'arn': elements[0],
        'partition': elements[1],
        'service': elements[2],
        'region': elements[3],
        'account': elements[4],
        'resource': elements[5],
        'resource_type': None
    }
    if '/' in result['resource']:
        result['resource_type'], result['resource'] = result['resource'].split('/',1)
    elif ':' in result['resource']:
        result['resource_type'], result['resource'] = result['resource'].split(':',1)
    return result

def get_type_from_url(url):
    """get either HLS or DASH Channel Assembly Packaging Group Type from url"""
    extension = os.path.splitext(urlparse(url).path)[1]
    if extension == '.m3u8':
        stream_type = 'HLS'
    elif extension == '.mpd':
        stream_type = 'DASH'
    else:
        stream_type = None
        raise KeyError(f"{extension} is not a valid extension. Must be either m3u8 or mpd.")
    return stream_type

def create_package_configuration(manifest_url, packaging_configuration_id):
    """
    create Channel Assembly packaging configurations from MediaPackage Egress Endpoints list.
    List can only even contain 1 Egress Endpoint as is the default from the MediaPackage-vod Playable event
    """
    try:
        package_configuration = {
                'Path':urlparse(manifest_url).path,
                'SourceGroup':packaging_configuration_id,
                'Type':get_type_from_url(manifest_url)
            }
    except Exception as manifest_parse_error:
        package_configuration = None
        raise KeyError(
            f"{manifest_url} not a valid MediaTailor EgressEndpoint list."
        ) from manifest_parse_error

    return package_configuration

def update_packaging_configurations(vod_source):
    """
    update existing Vod Source with new Packaging Configurations. 
    Checks if SourceGroup already exists and overwrites.
    """
    new_http_packaging_configuration = vod_source.pop('HttpPackageConfigurations', [None])[0]
    http_packaging_configurations = [new_http_packaging_configuration]
    try:
        response = mediatailor.describe_vod_source(**vod_source)
        for existing_http_packaging_configuration in response['HttpPackageConfigurations']:
            if existing_http_packaging_configuration['SourceGroup'] != new_http_packaging_configuration['SourceGroup']:
                http_packaging_configurations.append(existing_http_packaging_configuration)
            else:
                logger.info('Deleting existing Packaging Configuration: %s', existing_http_packaging_configuration)
        vod_source['HttpPackageConfigurations'] = http_packaging_configurations
        logger.info('Updating Vod Source: %s', json.dumps(vod_source, default=str))
        response = mediatailor.update_vod_source(**vod_source)
    except mediatailor.exceptions.BadRequestException as bad_request_error:
        raise bad_request_error('Update Failed!', bad_request_error) from bad_request_error
    return response

def lambda_handler(event, context):
    """
    Main function. Will add a MediaTailor Channel Assembly Source from
    a MediaPackage-vod Assest when status is PLAYABLE
    """
    init_logging()
    logger.info('Event: %s', json.dumps(event, default=str))

    try:
        vod_source = {
            'VodSourceName':parse_arn(event['resources'][0])['resource'],
            'SourceLocationName':os.environ['MediaTailorSourceLocation'],
            'HttpPackageConfigurations':[
                create_package_configuration(
                    event['detail']['manifest_urls'][0],
                    event['detail']['packaging_configuration_id'])
            ]
        }
        logger.info('vod_source: %s', json.dumps(vod_source, default=str))
        response = mediatailor.create_vod_source(**vod_source)
    except mediatailor.exceptions.BadRequestException as bad_request_error:
        if "exists" in bad_request_error.response['Error']['Message']:
            logger.info(
                'Vod Source Already exists. Adding new Package Configuration: %s',
                json.dumps(vod_source['HttpPackageConfigurations'][0], default=str)
            )
            sleep(randint(10,100)/10) #sleep for a random time to prevent update collisions
            update_packaging_configurations(vod_source)
            response = mediatailor.describe_vod_source(
                VodSourceName=vod_source['VodSourceName'],
                SourceLocationName=vod_source['SourceLocationName']
            )
        else:
            raise bad_request_error

    return json.dumps(response, default=str)
