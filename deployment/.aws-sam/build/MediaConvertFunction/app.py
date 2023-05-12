import os
import logging
import json
import boto3
import xmltodict

logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', logging.INFO))

endpoint = boto3.client('mediaconvert').describe_endpoints()[
    'Endpoints'][0]['Url']
logger.info('Endpoint URL: %s', endpoint)
mediaconvert = boto3.client('mediaconvert', endpoint_url=endpoint)


def get_s3_object_tags(bucket, key):
    '''
    takes a bucket and key then returns the tags
    '''
    try:
        tags = boto3.client('s3').get_object_tagging(
            Bucket=bucket, Key=key)['TagSet']
        logger.info('Tags: %s', json.dumps(tags))
        return tags
    except Exception as error:
        logger.info('No Tags: %s', error)
        return None


def get_ad_offsets(tags):
    '''
    looks through s3 taglist and finds the ad_offsets
    '''

    possible_key_names = [
        os.environ.get('AdOffsetS3TagKeyName'),
        'ad_offsets',
        'AdOffsets',
        'adoffsets',
        'Adoffsets'
    ]

    try:
        for tag in tags:
            if tag['Key'] in possible_key_names:
                logger.info('Found ad_offset key name: %s', tag['Key'])
                try:
                    ad_offsets = tag['Value'].split(' ')
                    logger.info('ad_offsets: %s', ad_offsets)
                    return ad_offsets
                except Exception as error:
                    logger.info('Error splitting into list: %s', error)
                    return False
    except Exception as error:
        logger.info('Error getting ad_offsets: %s', error)

    return False


def generate_esam(ad_offsets):
    '''
    generates the esam xml for the ad_offsets
    '''

    response_signals = []
    conditioning_infos = []
    manifests_responses = []
    try:
        for index, ad_offset in enumerate(ad_offsets):
            logger.info('ad offset found (milliseconds): %s', ad_offset)
            ad_offset = round(float(ad_offset)/1000, 3)
            response_signals.append(
                {
                    '@acquisitionPointIdentity': 'AWSElementalMediaTailor',
                    '@acquisitionSignalID': index,
                    '@signalPointID': index,
                    '@action': 'create',
                    'sig:NPTPoint': {'@nptPoint': ad_offset},
                    'sig:SCTE35PointDescriptor': {
                        '@spliceCommandType': '06',
                        'sig:SegmentationDescriptorInfo': {
                            '@segmentEventId': index,
                            '@segmentTypeId': '52'
                        }
                    }
                }
            )
            conditioning_infos.append(
                {
                    '@startOffset': f"PT{ad_offset}S",
                    '@acquisitionSignalIDRef': index,
                    '@duration': 'PT0S',
                    'Segment': 'PT0S'
                }
            )
            manifests_responses.append(
                {
                    '@acquisitionPointIdentity': 'AWSElementalMediaTailor',
                    '@acquisitionSignalID': index,
                    '@duration': 'PT0S',
                    '@dataPassThrough': 'true',
                    'SegmentModify': {
                        'FirstSegment': {
                            'Tag': [
                                        {'@value': '#EXT-X-CUE-OUT:0'},
                                        {'@value': '#EXT-X-CUE-IN'}
                            ]
                        }
                    }
                }
            )            

        scc_xml = {
            'SignalProcessingNotification': {
                '@xmlns': 'urn:cablelabs:iptvservices:esam:xsd:signal:1',
                '@xmlns:sig': 'urn:cablelabs:md:xsd:signaling:3.0',
                '@xmlns:common': 'urn:cablelabs:iptvservices:esam:xsd:common:1',
                '@xmlns:xsi': 'http://www.w3.org/2001/XMLSchema-instance',
                'common:BatchInfo': {'@batchId': 'abcd', 'common:Source': {'@xsi:type': 'content:MovieType'}},
                'ResponseSignal': response_signals,
                'ConditioningInfo': conditioning_infos
            }
        }

        mcc_xml = {
            'ManifestConfirmConditionNotification': {
                '@xmlns': 'http://www.cablelabs.com/namespaces/metadata/xsd/confirmation/2',
                'ManifestResponse': manifests_responses
            }
        }
    except Exception as error:
        logger.info('Error generating esam xml: %s', error)

    esam = {
        'ManifestConfirmConditionNotification': {
            'MccXml': xmltodict.unparse(mcc_xml, pretty=False)
        },
        'ResponseSignalPreroll': 0,
        'SignalProcessingNotification': {
            'SccXml': xmltodict.unparse(scc_xml, pretty=False)
        }
    }

    return esam


def format_template_for_new_job(template, input_file, esam=None, ad_offsets=False):
    '''
    takes a MediaConvert template and 
    input_file file then returns a job object
    '''
    template['JobTemplate'] = template['Name']
    template['Role'] = os.environ['MediaConvertTranscodeRoleArn']
    if ad_offsets:
        template['Tags'] = {'AdOffsets': ' '.join(ad_offsets)}
        template['UserMetadata'] = {'AdOffsets': ' '.join(ad_offsets)}
    template['Settings']['Inputs'][0]['FileInput'] = input_file
    del template['Description'], template['Category'], template['Name'], template[
        'Arn'], template['CreatedAt'], template['LastUpdated'], template['Type']

    if esam:
        logger.info('ESAM: %s', esam)
        template['Settings']['Esam'] = esam

    logger.info('MediaConvert Job JSON: %s', json.dumps(template))

    return template


def lambda_handler(event, context):
    logger.debug('Event: %s', json.dumps(event, default=str))
    input_file_bucket = event['detail']['bucket']['name']
    input_file_key = event['detail']['object']['key']
    input_file_tags = get_s3_object_tags(input_file_bucket, input_file_key)
    ad_offsets = get_ad_offsets(input_file_tags)
    esam = None
    if ad_offsets:
        esam = generate_esam(ad_offsets)

    template = mediaconvert.get_job_template(
        Name=os.environ['MediaConvertJobTemplate'])['JobTemplate']

    input_file = f"s3://{input_file_bucket}/{input_file_key}"

    job = mediaconvert.create_job(
        **format_template_for_new_job(template, input_file, esam, ad_offsets))['Job']
    result = {'Status': job['Status'], 'Id': job['Id']}
    logger.info('Result: %s', json.dumps(result))

    return result
