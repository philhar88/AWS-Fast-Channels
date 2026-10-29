#!/bin/bash
# Overlay O4 (FAST Channels). The guidance's amt-bidder/copy-bidder-files.sh runs this from the PBS-Java tree when the
# image is built with INCLUDE_AMT_BIDDER=true, the guidance's only injection point for code. It copies the fast-ctv
# module (pbs/fast-ctv in the FAST Channels repository) into the tree, where Spring's component scan of
# org.prebid.server registers it; hooks.fast-ctv.enabled=true in the Prebid Server configuration then activates it.
set -euo pipefail
src=./amt-bidder/fast-ctv/src/main/java/org/prebid/server/hooks/modules/fast/ctv
dst=./src/main/java/org/prebid/server/hooks/modules/fast/ctv
if [ ! -d "$src" ]; then
  echo "copy-fast-ctv: no module sources at $src" >&2
  exit 1
fi
mkdir -p "$dst"
cp -rv "$src/." "$dst/"
