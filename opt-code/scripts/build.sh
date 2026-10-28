#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

prepare_environment

jobs=${1:-${BUILD_JOBS:-32}}
[[ ${jobs} =~ ^[1-9][0-9]*$ ]] || die "build job count must be a positive integer"

printf 'Building the OpenROAD GRT overlay with %s jobs...\n' "${jobs}"
run_contest build "${jobs}"
