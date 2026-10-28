#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

prepare_environment
require_bp_fe

label=${1:-baseline}
[[ ${label} =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid run label: ${label}"

printf 'Running %s baseline as label %s...\n' "${CASE_NAME}" "${label}"
run_contest run-grt "${CASE_NAME}" "${CONTAINER_INPUT}" "${label}"

printf 'GRT output: %s/output/%s/%s/5_1_grt.odb\n' \
  "${OPEN3DBENCH_ROOT}" "${CASE_NAME}" "${label}"
