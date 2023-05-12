#### Install SAM
Follow instructions here:
https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html

#### Run any scripts (optional)
Follow instructions in the `sripts/SCRIPTS.md` to modify the deployment file if necessary. This is not a required step.
#### Build SAM project
Make sure you are in the `deployment` directory.
```bash
sam build --template-file fast-on-aws.deployment
```
#### Deploy SAM project
```bash
sam deploy --guided
```
You will be asked for the following inputs:
##### StackName
your friendly stack name, for example `FAST-Platform`
##### AWS Region
AWS Region to deploy to
##### Parameter AdServerUrl
Ad server URL provided by your Ad Decision Server provider. Leave default for dummy ad server
##### Parameter EmailAddress
an email address used to notify on content as it passes through the workflow
##### Parameter S3BucketName
friendly name for the S3 buckets generated (input and output). For example: `content`

(leave all other options as default)

This process can take 15-30 minutes. 

#### Test Deployment
Once the Stack has deployed, you will see a list of Outputs. The `VideoSourceBucket` Output will have a value of the s3 bucket name to upload your media content to. Start off by uploading some test content.

You can then open the AWS Console and navigate to the MediaConvert Service. Here you should see the file you uploaded being transcoded.

Once complete, you can navigate to the MediaTailor Service and locate the Channel named `-SampleChannel`. Here you can schedule the piece of content uploaded.