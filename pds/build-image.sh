#!/bin/bash
# Build and push the stage's PDS sidecar image (x86) on CodeBuild: the DemandBuild project runs `bash build-image.sh`
# in the context zip that scripts/_demand.py uploads (this script and context/, the Dockerfile and site/).
# Environment: REPOSITORY_URI (the sidecar's repository, overriding the project's) and IMAGE_TAG.
set -euo pipefail

registry="${REPOSITORY_URI%%/*}"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$registry"
docker build --platform linux/amd64 --tag "${REPOSITORY_URI}:${IMAGE_TAG}" context
docker push "${REPOSITORY_URI}:${IMAGE_TAG}"
echo "pushed ${REPOSITORY_URI}:${IMAGE_TAG}"
