import os

# Powertools reads these at import time; set test defaults before any handler module is imported.
os.environ.setdefault("POWERTOOLS_METRICS_NAMESPACE", "FastChannelsTest")
os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "fast-channels-test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
