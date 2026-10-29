#!/bin/bash
# Overlay O4 (FAST Channels). The guidance's amt-bidder/copy-bidder-files.sh runs this from the PBS-Java tree when the
# image is built with INCLUDE_AMT_BIDDER=true, the guidance's only injection point for code. It copies the fast-ctv
# module (pbs/fast-ctv in the FAST Channels repository) into the tree, where Spring's component scan of
# org.prebid.server registers it; hooks.fast-ctv.enabled=true in the Prebid Server configuration then activates it.
# On a Prebid Server Java 4.x tree it also puts amt-bidder/pbs-4/AmtConfiguration.java over the guidance's (4.x
# removed the user-syncer creator that configuration calls).
set -euo pipefail
src=./amt-bidder/fast-ctv/src/main/java/org/prebid/server/hooks/modules/fast/ctv
dst=./src/main/java/org/prebid/server/hooks/modules/fast/ctv
if [ ! -d "$src" ]; then
  echo "copy-fast-ctv: no module sources at $src" >&2
  exit 1
fi
mkdir -p "$dst"
cp -rv "$src/." "$dst/"
# The project's version is the pom's first numeric <version>; sed stops there (no pipe: under pipefail, `| head`
# can kill the producer with SIGPIPE and fail the build).
version=$(sed -n '/^ *<version>[0-9]/{s:^ *<version>\([0-9][^<]*\)</version>.*:\1:p;q;}' pom.xml)
case "$version" in
  4.*)
    cp -v ./amt-bidder/pbs-4/AmtConfiguration.java ./src/main/java/org/prebid/server/spring/config/bidder/AmtConfiguration.java
    ;;
esac
