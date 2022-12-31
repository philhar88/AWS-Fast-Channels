import boto3
import os
from urllib.parse import urlparse
import json
import logging
from random import randint
from time import sleep

logger = logging.getLogger()

def init_logging():
    try:
        logger.setLevel(logging.os.environ['LogLevel'].upper())
    except:
        logger.setLevel(logging.INFO)

init_logging()

mediatailor = boto3.client('mediatailor')

def parse_arn(arn):
    # http://docs.aws.amazon.com/general/latest/gr/aws-arns-and-namespaces.html
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
    extension = os.path.splitext(urlparse(url).path)[1]
    if extension == '.m3u8':
        return 'HLS'
    elif extension == '.mpd':
        return 'DASH'
    else:
        raise KeyError(f"{extension} is not a valid extension. Must be either m3u8 or mpd.")
        return None

def create_package_configuration(manifest_url, packaging_configuration_id):
    package_configurations = []
    try:
        package_configuration = {
                'Path':urlparse(manifest_url).path,
                'SourceGroup':packaging_configuration_id,
                'Type':get_type_from_url(manifest_url)
            }
        return package_configuration
    except:
        raise KeyError(f"{manifest_url} not a valid MediaTailor EgressEndpoint list.")
        return None

def lambda_handler(event, context):
    logger.info('Event: %s', json.dumps(event))
    try:
        VodSource = {
            'VodSourceName':parse_arn(event['resources'][0])['resource'],
            'SourceLocationName':os.environ['MediaTailorSourceLocation'],
            'HttpPackageConfigurations':[
                create_package_configuration(
                    event['detail']['manifest_urls'][0], 
                    event['detail']['packaging_configuration_id'])
            ]
        }
        logger.info('VodSource: %s', VodSource)
        mediatailor.create_vod_source(**VodSource)
    except mediatailor.exceptions.BadRequestException as error:
        if "exists" in error.response['Error']['Message']:
            sleep(randint(10,100)/10) #sleep for a random time to prevent update collisions
            try:
                HttpPackageConfiguration = VodSource.pop('HttpPackageConfigurations', [None])[0]
                response = mediatailor.describe_vod_source(**VodSource)
                VodSource['HttpPackageConfigurations'] = response['HttpPackageConfigurations']
                VodSource['HttpPackageConfigurations'].append(HttpPackageConfiguration)
                VodSource['HttpPackageConfigurations'] = VodSource['HttpPackageConfigurations']
                logger.info('Vod Source Already exists. Updating with new HttpPackageConfigurations List: %s', json.dumps(VodSource['HttpPackageConfigurations']))
                mediatailor.update_vod_source(**VodSource)
                HttpPackageConfiguration = VodSource.pop('HttpPackageConfigurations', [None])[0]
                sleep(randint(10,20)/10)
            except:
                raise
        else:
            raise error.response['Error']['Message']
    except Exception as error:
        raise error

    try:
        response = mediatailor.describe_vod_source(VodSourceName=VodSource['VodSourceName'],SourceLocationName=VodSource['SourceLocationName'])
    except:
        response = None

    return response
