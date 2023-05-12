# Scripts
This is a collection of useful scripts to aid in the generation of the CloudFormation template.

## generate_presets.py
This script will read `presets.csv` from the same directory and generato a `template.yaml` file output containing the following CloudFormation Resources:
 - MediaConvert Presets
 - MediaConvert Job Template

You may edit the contents of `presets.csv` file (copy from the assets folder to working directory of script) to define a custom bit-rate ladder for encoding. You must replace the existing MediaConvert Preset and MediaConver Job Template resources in the main deployment file with what is output in the `template.yaml` file.

this script requires the troposphere package. You can install this by running the following command:
```bash
pip3 install troposphere
```

the output will automatically be written to `deployment/mediaconvert.deployment`. This will then be used as a nested stack application within the primary deployment file.