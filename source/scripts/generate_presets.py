import csv
from troposphere import Template, Parameter, Sub, Ref, GetAtt, Output
import troposphere.mediaconvert as mediaconvert


def get_codec_settings(codec, bitrate, qvbr):
    '''
    gets the codec settings for the preset from the csv
    only avc and hevc are supported
    '''
    CodecSettings = {
        "Codec": ""
    }
    if codec == "hevc" or codec == "h265":
        CodecSettings['Codec'] = "H_265"
        CodecSettings['H265Settings'] = {
            "MaxBitrate": int(bitrate),
            "RateControlMode": "QVBR",
            "QvbrSettings": {"QvbrQualityLevel": int(qvbr)},
        }
    elif codec == "avc" or codec == "h264":
        CodecSettings['Codec'] = "H_264"
        CodecSettings['H264Settings'] = {
            "MaxBitrate": int(bitrate),
            "RateControlMode": "QVBR",
            "QvbrSettings": {"QvbrQualityLevel": int(qvbr)},
        }
    else:
        CodecSettings = None
    
    return CodecSettings


def generate_job_outputs(presets):
    '''
    iterates over existing resources and generates job outputs
    checks if resources is a MediaConvert preset type
    '''
    outputs = []
    count = 1
    for preset in presets['Resources']:
        if preset.startswith("MediaConvertPreset"):
            output = {
                "Preset": Ref(preset), 
                "NameModifier": "_" + str(count)
                }
            outputs.append(output)
            count = count + 1
    
    return outputs


# Open file
with open("../../assets/presets.csv", mode="r", encoding="utf-8-sig") as presets:

    # Create reader object by passing the file
    # object to DictReader method
    presets = csv.DictReader(presets)

    # Iterate over each row in the csv file
    # using reader object

    outputs = {"Resources": {}}

    template = Template()

    previous_name = None

    template.set_transform("AWS::Serverless-2016-10-31")
    template.set_version("2010-09-09")
    template.add_parameter(
        Parameter(
            "VideoDestinationBucket",
            Type="String",
            Description="The name of the S3 bucket where the video will be stored",
        )
    )
    template.add_parameter(
        Parameter(
            "StackName",
            Type="String",
            Description="Name of Parent Stack",
        )
    )
    Queue = mediaconvert.Queue("MediaConvertQueue")
    Queue.Name = Sub("${StackName}-Queue")
    template.add_resource(Queue)

    for row in presets:
        PresetName = 'MediaConvertPreset'
        if row["type"].lower() == "video":
            PresetName = PresetName + row["height"] + row["codec"].upper() + row["bitrate"]
            preset = mediaconvert.Preset(PresetName)
            preset.Description = "{}x{} resolution at {}Kbit/s in {}".format(
                row["width"], row["height"], row["bitrate"], row["codec"].upper()
            )
            preset.SettingsJson = {
                "VideoDescription": {
                    "Width": int(row["width"]),
                    "Height": int(row["height"]),
                    "CodecSettings": get_codec_settings(
                        row["codec"],
                        int(row["bitrate"]) * 1000,
                        row["qvbr"],
                    ),
                },
                "ContainerSettings": {"Container": "M3U8"},
            }

        if row["type"].lower() == "audio":
            PresetName = PresetName + row["codec"].upper() + row["bitrate"]
            preset = mediaconvert.Preset(PresetName)
            preset.Description = "{}Kbit/s in {}".format(
                row["bitrate"], row["codec"].upper()
            )
            preset.SettingsJson = {
                "AudioDescriptions": [
                    {
                        "AudioTypeControl": "FOLLOW_INPUT",
                        "AudioSourceName": "Audio Selector 1",
                        "CodecSettings": {
                            "Codec": "AAC",
                            "AacSettings": {
                                "AudioDescriptionBroadcasterMix": "NORMAL",
                                "Bitrate": int(row["bitrate"]) * 1000,
                                "RateControlMode": "CBR",
                                "CodecProfile": "LC",
                                "CodingMode": "CODING_MODE_2_0",
                                "RawFormat": "NONE",
                                "SampleRate": 48000,
                                "Specification": "MPEG4",
                            },
                        },
                        "LanguageCodeControl": "FOLLOW_INPUT",
                    }
                ],
                "ContainerSettings": {
                    "Container": "M3U8",
                    "M3u8Settings": {},
                },
            }
        preset.Name = Sub("${StackName}-" + PresetName)
        preset.Category = Ref("StackName")
        if previous_name:
            preset.DependsOn = previous_name
        template.add_resource(preset)
        previous_name = PresetName

    JobTemplate = mediaconvert.JobTemplate("MediaConvertJobTemplate")
    JobTemplate.Queue = GetAtt(Queue, "Arn")
    JobTemplate.AccelerationSettings = mediaconvert.AccelerationSettings(
        Mode="PREFERRED"
    )
    JobTemplate.Description = Sub("Job template for ${StackName}")
    JobTemplate.Category = Ref("StackName")
    JobTemplate.Name = Sub("${StackName}-MediaConvertJobTemplate")
    JobTemplate.SettingsJson = {
        "TimecodeConfig": {"Source": "ZEROBASED"},
        "Inputs": [
            {
                "TimecodeSource": "ZEROBASED",
                "VideoSelector": {},
                "AudioSelectors": {"Audio Selector 1": {"DefaultSelection": "DEFAULT"}},
                "CaptionSelectors": {
                    "Captions Selector 1": {
                        "SourceSettings": {
                            "SourceType": "EMBEDDED",
                            "EmbeddedSourceSettings": {},
                        }
                    }
                },
            }
        ],
        "OutputGroups": [
            {
                "Name": "Apple HLS",
                "Outputs": generate_job_outputs(template.to_dict()),
                "OutputGroupSettings": {
                    "Type": "HLS_GROUP_SETTINGS",
                    "HlsGroupSettings": {
                        "SegmentLength": 6,
                        "MinSegmentLength": 0,
                        "Destination": Sub("s3://${VideoDestinationBucket}/"),
                    },
                },
                "CustomName": "Output for MediaPackage",
            }
        ],
    }
    template.add_resource(JobTemplate)
    template.add_output(
        Output(
        "JobTemplate",
        Description="MediaConvert Job Template",
        Value=Ref(JobTemplate),
        )
    )
    template.add_output(
        Output(
        "QueueArn",
        Description="MediaConvert Queue ARN",
        Value=GetAtt(Queue, "Arn"),
        )
    )
with open("../../deployment/mediaconvert.deployment", "w") as f:
    f.write(template.to_yaml())
