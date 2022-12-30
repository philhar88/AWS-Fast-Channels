# AWS-Fast-Channels

This SAM project deploys an end to end FAST channel solution primarly using MediaTailor's Channel Assembly feature to produce linear HLS and DASH streams. Rather than running a Live Encoding processes, this solution prepares the media as if it were VOD and then uses playlist manipulation available through Channel Assembly to create live streams.

At deployment, you will first define two buckets. The input bucket is where you can drop your mezzanine input VODs (such as MP4s, MXF, MOV etc). This will trigger a MediaConvert job to prepare your media into HLS, DASH and CMAF streaming formats. Once the MediaConvert job is complete, the output will be ingested into MediaPackage-Vod and MediaTailor-ChannelAssembly.

Additionally, a custom resource of the `AdBreakSlates` will be generated upon deployment. This will allow you to insert ad breaks to your channel output. You can configure additional AdBreak times by modifying the `template.yaml` file

A `SampleChannel` is also created. This is where you can start adding Programs to create a schedule. See the MediaTailor Channel Assembly Quickstart guide to adding Programs for more information:
https://docs.aws.amazon.com/mediatailor/latest/ug/channel-assembly-getting-started.html#ca-getting-started-create-programs

An AdServer Url can also be defined if you already have an Ad Server defined.

## Features

- Fully automated set-up of S3, MediaConvert, MediaPackage, CloudFront and MediaTailor
- Follows best practice guidelines including securing MediaPackage Origin and Caching configuration
- Custom Resource to automatically generate AdBreak slates
- Alerts to media processing via email

## Deployment

To deploy this project run

```bash
  sam deploy --guided
```
Make sure you have SAM installed: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html#install-sam-cli-instructions

## FAQs
#### How much will this solution cost?

This solution will incur the following costs as per your AWS usage rates:
 - S3 Storage
 - MediaPackage Data Transfer Out
 - MediaConvert Transcode
 - MediaTailor Channel Assembly
 - MediaTailor Ad Insertion
 - CloudFront CDN Bytes Out

#### How can I change the bit rate ladder?

within the template.yaml you can configure the Presets with `Type: AWS::MediaConvert::Preset`. Make sure to also update the job template (`Type: AWS::MediaConvert::JobTemplate`) if you remove or add presets. Included in the repo is a script in the `MediaConvertJobTemplate` directory which will generate Presets and associated Template for you. Simply modify the included `presets.csv` and execute `generate_presets.py`. You will need the python3 package `troposphere` installed. 

#### How can I delete the resources created by this SAM?

firstly make sure the following resources are empty (i.e. contain no resources but still exist)
 - SourceLocation (i.e delete all VodSources)
 - input and output s3 buckets (i.e. delete all objects in the bucket)
 - MediaPackage PackagingGroup (i.e. delete all VOD Assets associated with the given Packaging Group)

#### How can I access the streams?

The SAM project will emit an Output with the prefix URLs to the `SampleChannel` Ad Insertion hostname. Make sure the channel is in running before you request the stream.