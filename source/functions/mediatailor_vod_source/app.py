import os
from urllib.parse import urlparse
import json
import logging
from random import randint
from time import sleep
import boto3

# define clients required for script
mediatailor = boto3.client('mediatailor')
events = boto3.client("events")
logger = logging.getLogger()
logger.setLevel(logging.os.environ.get('LOG_LEVEL', 'INFO'))


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
        result['resource_type'], result['resource'] = result['resource'].split(
            '/', 1)
    elif ':' in result['resource']:
        result['resource_type'], result['resource'] = result['resource'].split(
            ':', 1)
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
        raise KeyError(
            f"{extension} is not a valid extension. Must be either m3u8 or mpd.")
    return stream_type


def create_package_configuration(manifest_url, packaging_configuration_id):
    """
    create Channel Assembly packaging configurations from MediaPackage Egress Endpoints list.
    List can only even contain 1 Egress Endpoint as is the default from the MediaPackage-vod Playable event
    """
    try:
        package_configuration = {
            'Path': urlparse(manifest_url).path,
            'SourceGroup': packaging_configuration_id,
            'Type': get_type_from_url(manifest_url)
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
    new_http_packaging_configuration = vod_source.pop(
        'HttpPackageConfigurations', [None])[0]
    http_packaging_configurations = [new_http_packaging_configuration]
    try:
        response = mediatailor.describe_vod_source(**vod_source)
        for existing_http_packaging_configuration in response['HttpPackageConfigurations']:
            if existing_http_packaging_configuration['SourceGroup'] != new_http_packaging_configuration['SourceGroup']:
                http_packaging_configurations.append(
                    existing_http_packaging_configuration)
            else:
                logger.info('Deleting existing Packaging Configuration: %s',
                            existing_http_packaging_configuration)
        vod_source['HttpPackageConfigurations'] = http_packaging_configurations
        logger.info('Updating Vod Source: %s',
                    json.dumps(vod_source, default=str))
        response = mediatailor.update_vod_source(**vod_source)
    except mediatailor.exceptions.BadRequestException as bad_request_error:
        raise bad_request_error(
            'Update Failed!', bad_request_error) from bad_request_error
    return response


def get_tags(asset_arn):
    '''
    executes a mediapackage tags get call and adds the tags to the vod source
    '''
    client = boto3.client('mediapackage-vod')
    try:
        tags = client.list_tags_for_resource(ResourceArn=asset_arn)['Tags']
        logger.info('Tags: %s', json.dumps(tags))
        return tags
    except Exception as error:
        logger.error('Error retreiving tags: %s', error)
        tags = {}

    return tags


def lambda_handler(event, context):
    """
    Main function. Will add a MediaTailor Channel Assembly Source from
    a MediaPackage-vod Assest when status is PLAYABLE
    """

    logger.debug('Event: %s', json.dumps(event, default=str))

    try:
        vod_source = {
            'VodSourceName': parse_arn(event['resources'][0])['resource'],
            'SourceLocationName': os.environ['MediaTailorSourceLocation'],
            'HttpPackageConfigurations': [
                create_package_configuration(
                    event['detail']['manifest_urls'][0],
                    event['detail']['packaging_configuration_id'])
            ]
        }
        tags = get_tags(event['resources'][0])
        if tags:
            logger.info('Adding Tags to Vod Source: %s',
                        vod_source['VodSourceName'])
            vod_source['Tags'] = tags
        logger.info('vod_source: %s', json.dumps(vod_source, default=str))
        response = mediatailor.create_vod_source(**vod_source)
    except mediatailor.exceptions.BadRequestException as bad_request_error:
        if "exists" in bad_request_error.response['Error']['Message']:
            logger.info(
                'Vod Source Already exists. Adding new Package Configuration: %s',
                json.dumps(
                    vod_source['HttpPackageConfigurations'][0], default=str)
            )
            # sleep for a random time to prevent update collisions
            sleep(randint(10, 100)/10)
            if tags:
                # update_vod_source does not support adding tags
                del vod_source['Tags']
            update_packaging_configurations(vod_source)
            response = mediatailor.describe_vod_source(
                VodSourceName=vod_source['VodSourceName'],
                SourceLocationName=vod_source['SourceLocationName']
            )
        else:
            raise bad_request_error

    try:
        # sleep for a random time to prevent update collisions
        sleep(5)
        logger.info('Create Program in Sample Channel...')
        schedule = mediatailor.get_channel_schedule(
            ChannelName=os.environ['MediaTailorChannelName'],
            DurationMinutes='1',
            MaxResults=1
        )

        program = {
            'ChannelName': os.environ['MediaTailorChannelName'],
            'ProgramName': vod_source['VodSourceName'],
            'SourceLocationName': vod_source['SourceLocationName'],
            'VodSourceName': vod_source['VodSourceName'],
            'ScheduleConfiguration': {
                'Transition': {
                    'Type': 'RELATIVE',
                    'RelativePosition': 'BEFORE_PROGRAM'
                }
            }
        }

        try:
            relative_program = schedule['Items'][0]['ProgramName']
            program['ScheduleConfiguration']['Transition']['RelativeProgram'] = relative_program
        except IndexError:
            logger.info('No previous Programs in Channel. Creating new Program...')

        if tags.get('AdOffsets', False):
            offsets = tags['AdOffsets'].split(' ')
            ad_breaks = []
            for index, offset in enumerate(offsets):
                ad_breaks.append(
                    {
                        'OffsetMillis': int(offset),
                        'MessageType': 'SPLICE_INSERT',
                        'SpliceInsertMessage': {
                            'AvailNum': int(index),
                            'AvailsExpected': 1,
                            'SpliceEventId': int(index),
                            'UniqueProgramId': int(index)
                        },
                        'Slate': {
                            'SourceLocationName': os.environ['MediaTailorSourceLocation'],
                            'VodSourceName': 'AdBreakSlate30000',
                        }
                    }
                )
            program['AdBreaks'] = ad_breaks

        logger.info('Program: %s', json.dumps(program, default=str))
        response = mediatailor.create_program(**program)

    except mediatailor.exceptions.BadRequestException as bad_request_error:
        if "exists" in bad_request_error.response['Error']['Message']:
            logger.info('Program Already exists. Deleting and re-creating...')
            mediatailor.delete_program(
                ChannelName=os.environ['MediaTailorChannelName'],
                ProgramName=vod_source['VodSourceName']
            )
            sleep(5)
            response = mediatailor.create_program(**program)

    except Exception as error:
        logger.error('Error creating Program: %s', error)

    return json.dumps(response, default=str)
