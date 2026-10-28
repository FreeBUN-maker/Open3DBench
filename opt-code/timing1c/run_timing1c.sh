#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)/common.sh"

prepare_environment
require_bp_fe

label=${1:-timing1c}
[[ ${label} =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid run label: ${label}"

algorithm_dir="${OPT_CODE_ROOT}/timing1c"
[[ -f "${algorithm_dir}/prepare_timing_mls.tcl" ]] || die \
  "timing1c preparation script is missing: ${algorithm_dir}/prepare_timing_mls.tcl"

CONTEST_EXTRA_DOCKER_ARGS=(
  -v "${algorithm_dir}:${CONTAINER_ROOT}/opt-code/timing1c:ro"
  -e "GRT_PREPARE_TCL=${CONTAINER_ROOT}/opt-code/timing1c/prepare_timing_mls.tcl"
  -e TMLS_MAX_NETS=1
  -e TMLS_TARGET_NET=net1388
)

printf 'Running %s timing1c as label %s...\n' "${CASE_NAME}" "${label}"
run_contest run-grt "${CASE_NAME}" "${CONTAINER_INPUT}" "${label}"

printf 'GRT output: %s/output/%s/%s/5_1_grt.odb\n' \
  "${OPEN3DBENCH_ROOT}" "${CASE_NAME}" "${label}"
