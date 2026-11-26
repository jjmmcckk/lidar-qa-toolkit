#!/usr/bin/env bash
# Download the Autzen sample point cloud from the PDAL project's test data (about 3.7 MB)
set -euo pipefail
mkdir -p "$(dirname "$0")/data"
curl -L -o "$(dirname "$0")/data/autzen_trim.las" \
  https://raw.githubusercontent.com/PDAL/PDAL/master/test/data/las/autzen_trim.las
echo "Saved examples/data/autzen_trim.las"
