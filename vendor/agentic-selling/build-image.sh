#!/bin/bash
# Build and push one of the agentic-selling module's images (arm64) on CodeBuild: the selling image project runs
# `bash build-image.sh` in the context zip that scripts/_selling.py uploads (this script, and for the seller its
# bridge layer under seller/). Each upstream repository is cloned at its pinned commit (PIN.toml) and built with its
# own Dockerfile, unmodified.
# Environment: IMAGE (seller, buyer or governance), REPOSITORY_URI, IMAGE_TAG, SOURCE_REPOSITORY, SOURCE_COMMIT, and
# for the buyer and the governance agent SOURCE_PATH (the agent's directory), BASE and BASE_DIGEST (the base image the
# Dockerfile names by tag, pulled by digest).
set -euo pipefail

registry="${REPOSITORY_URI%%/*}"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$registry"

git clone --quiet --filter=blob:none "$SOURCE_REPOSITORY" source
git -C source checkout --quiet "$SOURCE_COMMIT"
head=$(git -C source rev-parse HEAD)
if [ "$head" != "$SOURCE_COMMIT" ]; then
  echo "checked out ${head}, not the pinned ${SOURCE_COMMIT}" >&2
  exit 1
fi

pin_base() {
  # The Dockerfile's FROM names a floating tag: pull the pinned digest and give it that tag locally.
  docker pull --quiet --platform linux/arm64 "${BASE}@${BASE_DIGEST}"
  docker tag "${BASE}@${BASE_DIGEST}" "$BASE"
}

image="${REPOSITORY_URI}:${IMAGE_TAG}"
case "$IMAGE" in
  seller)
    docker build --platform linux/arm64 --tag fast-seller-base source
    docker build --platform linux/arm64 --build-arg BASE=fast-seller-base --tag "$image" seller
    ;;
  buyer)
    # The A2A runtime symlinks most of its modules to its parent directory: build from a dereferenced copy.
    mkdir context
    cp -rL "source/${SOURCE_PATH}/." context/
    rm -rf context/__pycache__
    pin_base
    docker build --platform linux/arm64 --pull=false --tag "$image" context
    ;;
  governance)
    pin_base
    docker build --platform linux/arm64 --pull=false --tag "$image" "source/${SOURCE_PATH}"
    ;;
  *)
    echo "unknown image ${IMAGE}" >&2
    exit 1
    ;;
esac
docker push "$image"
echo "pushed ${image}"
