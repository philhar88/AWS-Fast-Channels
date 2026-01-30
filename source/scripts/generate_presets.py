#!/usr/bin/env python3
"""
Generate MediaConvert presets and job template from presets.csv.

This script reads the presets.csv file and generates a CloudFormation-compatible
MediaConvert deployment template with all necessary presets and job template.

Usage:
    cd source/scripts
    python3 generate_presets.py

The generated template is written to deployment/mediaconvert.deployment
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from troposphere import GetAtt, Output, Parameter, Ref, Sub, Template
import troposphere.mediaconvert as mediaconvert


def get_codec_settings(codec: str, bitrate: int, qvbr: int) -> dict[str, Any] | None:
    """
    Generate codec settings for MediaConvert presets.
    
    Supports AVC (H.264) and HEVC (H.265) codecs with QVBR rate control.
    """
    codec_lower = codec.lower()
    
    if codec_lower in ("hevc", "h265"):
        return {
            "Codec": "H_265",
            "H265Settings": {
                "MaxBitrate": bitrate,
                "RateControlMode": "QVBR",
                "QvbrSettings": {"QvbrQualityLevel": qvbr},
                "CodecProfile": "MAIN_MAIN",
                "CodecLevel": "AUTO",
                "GopSize": 90,
                "GopSizeUnits": "FRAMES",
            },
        }
    elif codec_lower in ("avc", "h264"):
        return {
            "Codec": "H_264",
            "H264Settings": {
                "MaxBitrate": bitrate,
                "RateControlMode": "QVBR",
                "QvbrSettings": {"QvbrQualityLevel": qvbr},
                "CodecProfile": "HIGH",
                "CodecLevel": "AUTO",
                "GopSize": 90,
                "GopSizeUnits": "FRAMES",
            },
        }
    else:
        return None


def generate_job_outputs(presets: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate job template outputs from preset resources."""
    outputs = []
    count = 1
    
    for resource_name in presets.get("Resources", {}):
        if resource_name.startswith("MediaConvertPreset"):
            outputs.append({
                "Preset": Ref(resource_name),
                "NameModifier": f"_{count}",
            })
            count += 1
    
    return outputs


def main() -> None:
    """Main function to generate the MediaConvert template."""
    script_dir = Path(__file__).parent
    presets_file = script_dir / "../../assets/presets.csv"
    output_file = script_dir / "../../deployment/mediaconvert.deployment"

    template = Template()
    template.set_transform("AWS::Serverless-2016-10-31")
    template.set_version("2010-09-09")

    # Add parameters
    template.add_parameter(Parameter(
        "VideoDestinationBucket",
        Type="String",
        Description="The name of the S3 bucket where the video will be stored",
    ))
    
    template.add_parameter(Parameter(
        "StackName",
        Type="String",
        Description="Name of Parent Stack",
    ))

    # Create MediaConvert queue
    queue = mediaconvert.Queue("MediaConvertQueue")
    queue.Name = Sub("${StackName}-Queue")
    template.add_resource(queue)

    # Read presets from CSV
    previous_name = None
    
    with open(presets_file, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        
        for row in reader:
            preset_type = row.get("type", "").lower()
            codec = row.get("codec", "avc")
            bitrate = row.get("bitrate", "")
            
            if not bitrate:
                continue
            
            if preset_type == "video":
                height = row.get("height", "")
                width = row.get("width", "")
                qvbr = int(row.get("qvbr", 7))
                
                preset_name = f"MediaConvertPreset{height}{codec.upper()}{bitrate}"
                preset = mediaconvert.Preset(preset_name)
                preset.Description = f"{width}x{height} resolution at {bitrate}Kbit/s in {codec.upper()}"
                preset.SettingsJson = {
                    "VideoDescription": {
                        "Width": int(width),
                        "Height": int(height),
                        "CodecSettings": get_codec_settings(codec, int(bitrate) * 1000, qvbr),
                    },
                    "ContainerSettings": {"Container": "M3U8"},
                }
            
            elif preset_type == "audio":
                preset_name = f"MediaConvertPreset{codec.upper()}{bitrate}"
                preset = mediaconvert.Preset(preset_name)
                preset.Description = f"{bitrate}Kbit/s in {codec.upper()}"
                preset.SettingsJson = {
                    "AudioDescriptions": [
                        {
                            "AudioTypeControl": "FOLLOW_INPUT",
                            "AudioSourceName": "Audio Selector 1",
                            "CodecSettings": {
                                "Codec": "AAC",
                                "AacSettings": {
                                    "AudioDescriptionBroadcasterMix": "NORMAL",
                                    "Bitrate": int(bitrate) * 1000,
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
            else:
                continue

            preset.Name = Sub(f"${{StackName}}-{preset_name}")
            preset.Category = Ref("StackName")
            
            if previous_name:
                preset.DependsOn = previous_name
            
            template.add_resource(preset)
            previous_name = preset_name

    # Create job template
    job_template = mediaconvert.JobTemplate("MediaConvertJobTemplate")
    job_template.Queue = GetAtt(queue, "Arn")
    job_template.AccelerationSettings = mediaconvert.AccelerationSettings(Mode="PREFERRED")
    job_template.Description = Sub("Job template for ${StackName}")
    job_template.Category = Ref("StackName")
    job_template.Name = Sub("${StackName}-MediaConvertJobTemplate")
    job_template.SettingsJson = {
        "TimecodeConfig": {"Source": "ZEROBASED"},
        "Inputs": [
            {
                "TimecodeSource": "ZEROBASED",
                "VideoSelector": {},
                "AudioSelectors": {
                    "Audio Selector 1": {"DefaultSelection": "DEFAULT"}
                },
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
    template.add_resource(job_template)

    # Add outputs
    template.add_output(Output(
        "JobTemplate",
        Description="MediaConvert Job Template",
        Value=Ref(job_template),
    ))
    
    template.add_output(Output(
        "QueueArn",
        Description="MediaConvert Queue ARN",
        Value=GetAtt(queue, "Arn"),
    ))

    # Write template
    with open(output_file, "w") as f:
        f.write(template.to_yaml())
    
    print(f"Generated {output_file}")


if __name__ == "__main__":
    main()
