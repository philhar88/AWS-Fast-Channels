# Python3 program to read CSV file using DictReader

# Import necessary packages
import csv
import yaml
import json
from troposphere import Template, Parameter, Sub, Ref, GetAtt
import troposphere.mediaconvert as mediaconvert


def get_codec_settings(codec, bitrate, qvbr):
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
    outputs = []
    count = 1
    for preset in presets['Resources']:
        output = {
            "Preset": Ref(preset), 
            "NameModifier": "_" + str(count)
            }
        outputs.append(output)
        count = count + 1
    
    return outputs


# Open file
with open("presets.csv", mode="r", encoding="utf-8-sig") as presets:

    # Create reader object by passing the file
    # object to DictReader method
    presets = csv.DictReader(presets)

    # Iterate over each row in the csv file
    # using reader object

    outputs = {"Resources": {}}

    template = Template()

    for row in presets:
        if row["type"].lower() == "video":
            PresetName = row["height"] + row["codec"].upper() + row["bitrate"]
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
            PresetName = row["codec"].upper() + row["bitrate"]
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
        preset.Name = Sub("${AWS::StackName}_" + PresetName)
        preset.Category = Ref("AWS::StackName")
        template.add_resource(preset)

    JobTemplate = mediaconvert.JobTemplate("MediaConvertJobTemplate")
    JobTemplate.Queue = GetAtt("MediaConvertQueue", "Arn")
    JobTemplate.AccelerationSettings = mediaconvert.AccelerationSettings(
        Mode="PREFERRED"
    )
    JobTemplate.Description = Sub("Job template for ${AWS::StackName}")
    JobTemplate.Category = Ref("AWS::StackName")
    JobTemplate.Name = Sub("${AWS::StackName}-MediaConvertJobTemplate")
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

with open("template.yaml", "w") as f:
    f.write(template.to_yaml())