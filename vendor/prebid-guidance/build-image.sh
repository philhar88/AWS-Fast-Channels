#!/bin/bash
# Build and push the stage's Prebid Server image (x86) from the guidance's unmodified Dockerfile, on CodeBuild
# (the DemandBuild project runs `bash build-image.sh` in the context zip that prebid_guidance.py uploads: this
# script and context/, the guidance's deployment/ecr/prebid-server with its amt-bidder sources).
# Environment: REPOSITORY_URI (the project), IMAGE_TAG, PBS_JAVA_REPOSITORY, PBS_JAVA_TAG, PBS_JAVA_COMMIT and
# PBS_JDK, the Corretto major version for overlay O6 (scripts/prebid_guidance.py build-image, from
# vendor/prebid-guidance/PIN.toml).
set -euo pipefail

# The Dockerfile checks Prebid Server Java out by tag: refuse to build if the tag no longer names the pinned commit.
refs=$(git ls-remote "$PBS_JAVA_REPOSITORY" "refs/tags/${PBS_JAVA_TAG}" "refs/tags/${PBS_JAVA_TAG}^{}")
resolved=$(awk -v peeled="refs/tags/${PBS_JAVA_TAG}^{}" '$2 == peeled { print $1 }' <<<"$refs")
[ -n "$resolved" ] || resolved=$(awk '{ print $1; exit }' <<<"$refs")
if [ "$resolved" != "$PBS_JAVA_COMMIT" ]; then
  echo "Prebid Server Java ${PBS_JAVA_TAG} is ${resolved:-missing}, not the pinned ${PBS_JAVA_COMMIT}" >&2
  exit 1
fi

registry="${REPOSITORY_URI%%/*}"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$registry"
docker build --platform linux/amd64 --build-arg INCLUDE_AMT_BIDDER=true --build-arg "JDK=${PBS_JDK:-21}" \
  --tag "${REPOSITORY_URI}:${IMAGE_TAG}" context
docker push "${REPOSITORY_URI}:${IMAGE_TAG}"
echo "pushed ${REPOSITORY_URI}:${IMAGE_TAG}"
