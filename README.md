# AWS-Fast-Channels

[![CI/CD Pipeline](https://github.com/philhar88/AWS-Fast-Channels/actions/workflows/ci.yml/badge.svg)](https://github.com/philhar88/AWS-Fast-Channels/actions/workflows/ci.yml)

This SAM project deploys an end-to-end FAST (Free Ad-Supported Streaming TV) channel solution using AWS MediaTailor's Channel Assembly feature to produce linear HLS and DASH streams. Rather than running a Live Encoding process, this solution prepares media as VOD and uses playlist manipulation available through Channel Assembly to create 24/7 live streams.

## 🆕 What's New in v2.0 (2026)

- **4K & 1080p Support**: Full Ultra HD encoding ladder with HEVC/AVC codecs
- **Python 3.12 Runtime**: Modern Lambda runtime with improved performance
- **Enhanced Security**: Least-privilege IAM, KMS encryption, S3 hardening
- **CI/CD Pipeline**: GitHub Actions with lint, validate, build, and security scanning
- **Better Observability**: X-Ray tracing and structured logging
- **Improved Performance**: HTTP/3, optimized segment duration, higher Lambda memory

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│   S3 Input  │────▶│ MediaConvert │────▶│  S3 Output      │
│   Bucket    │     │   (Transcode)│     │  (HLS/DASH/CMAF)│
└─────────────┘     └──────────────┘     └────────┬────────┘
                                                   │
                                                   ▼
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│ MediaTailor │◀────│ MediaPackage │◀────│                 │
│  (SSAI)     │     │   VOD        │     │                 │
└──────┬──────┘     └──────────────┘     └─────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│                    CloudFront CDN                        │
│                  (HTTP/2 + HTTP/3)                       │
└─────────────────────────────────────────────────────────┘
```

## Features

- **Automated Media Pipeline**: S3 → MediaConvert → MediaPackage → MediaTailor
- **Frame-Accurate Ad Insertion**: ESAM-based SCTE-35 marker placement
- **Multiple Output Formats**: HLS, DASH, and CMAF packaging
- **4K/HDR Ready**: Encoding ladder supports up to 3840x2160 with HEVC
- **Secure by Default**: Encrypted storage, signed origins, least-privilege IAM
- **Infrastructure as Code**: Fully automated deployment via SAM/CloudFormation

## Prerequisites

- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) v1.100.0+
- Python 3.12+
- AWS CLI configured with appropriate credentials

## Deployment

### Quick Start

```bash
cd deployment
sam build --template-file fast-on-aws.deployment
sam deploy --guided --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND
```

### Required Parameters

| Parameter | Description |
|-----------|-------------|
| `EmailAddress` | Email for SNS notifications |
| `S3BucketName` | Base name for S3 buckets (will create input/output buckets) |

### Optional Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `AdServerUrl` | Demo VAST | Your ad decision server URL |
| `AdOffsetS3TagKeyName` | `AdOffsets` | S3 tag key for ad break offsets |
| `LogLevel` | `INFO` | Lambda log level |
| `Enable4KEncoding` | `true` | Enable 4K output in encoding ladder |
| `EnableHEVC` | `true` | Enable HEVC codec for 1080p+ |

## Usage

### Upload Content

Upload video files to the input bucket:

```bash
aws s3 cp my_video.mp4 s3://${INPUT_BUCKET}/
```

Supported input formats: MP4, MOV, MXF, MKV, AVI, TS, M2TS

### Add Ad Breaks

Tag your S3 objects with ad break positions (milliseconds):

```bash
aws s3api put-object-tagging \
  --bucket ${INPUT_BUCKET} \
  --key my_video.mp4 \
  --tagging 'TagSet=[{Key=AdOffsets,Value="30000 90000 120000"}]'
```

This places ad opportunities at 30s, 90s, and 120s.

### Access Playback URLs

After processing, you'll receive an email with playback URLs:

- **HLS**: `https://{mediatailor}/v1/master/.../hls.m3u8`
- **DASH**: `https://{mediatailor}/v1/dash/.../dash.mpd`
- **CMAF**: `https://{mediatailor}/v1/master/.../cmaf.m3u8`

## Encoding Ladder

| Resolution | Codec | Bitrate | Use Case |
|------------|-------|---------|----------|
| 3840x2160 | HEVC | 15 Mbps | 4K Premium |
| 3840x2160 | AVC | 12 Mbps | 4K Compatible |
| 1920x1080 | HEVC | 8 Mbps | Full HD Premium |
| 1920x1080 | AVC | 6 Mbps | Full HD |
| 1280x720 | AVC | 4.5 Mbps | HD |
| 1280x720 | AVC | 3 Mbps | HD Low |
| 960x540 | AVC | 2 Mbps | qHD |
| 768x432 | AVC | 1.1 Mbps | SD |
| 640x360 | AVC | 600 Kbps | Mobile |
| 416x234 | AVC | 300 Kbps | Low Bandwidth |

### Customize Encoding

Edit `assets/presets.csv` and regenerate:

```bash
cd source/scripts
pip install -r requirements.txt
python3 generate_presets.py
```

## Cost Considerations

This solution incurs charges for:

- **S3**: Storage for input/output media
- **MediaConvert**: Per-minute transcoding
- **MediaPackage**: Packaging and egress
- **MediaTailor**: Channel Assembly hours + Ad insertion
- **CloudFront**: CDN distribution

💡 **Tip**: Output bucket uses Intelligent Tiering to optimize storage costs.

## Cleanup

Before deleting the stack, empty these resources:

1. Delete all VodSources from the MediaTailor SourceLocation
2. Empty the input and output S3 buckets
3. Delete all VOD Assets from the MediaPackage PackagingGroup

Then delete the stack:

```bash
sam delete --stack-name <your-stack-name>
```

## Troubleshooting

### Channel Won't Delete

Channels must be stopped before deletion. The custom resource handles this automatically, but if manual intervention is needed:

```bash
aws mediatailor stop-channel --channel-name <channel-name>
aws mediatailor delete-channel --channel-name <channel-name>
```

### MediaConvert Job Fails

Check the MediaConvert console for detailed error messages. Common issues:
- Input file format not supported
- Insufficient IAM permissions
- Invalid ad offset values

### No Playback URL Email

Verify:
1. Email subscription is confirmed in SNS
2. Lambda function logs show successful execution
3. MediaPackage asset creation completed

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

This project is licensed under the MIT License - see the LICENSE file for details.
