#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

prepare_environment
require_bp_fe

label=${1:-}
[[ -n ${label} ]] || die "usage: $0 <run-label>"
[[ ${label} =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid run label: ${label}"

host_output="${OPEN3DBENCH_ROOT}/output/${CASE_NAME}/${label}"
host_odb="${host_output}/5_1_grt.odb"
host_reports="${OPEN3DBENCH_ROOT}/reports/${CASE_NAME}/${label}"

[[ -f "${host_odb}" ]] || die "GRT result is missing: ${host_odb}"
mkdir -p "${host_reports}"

printf 'Evaluating %s run %s...\n' "${CASE_NAME}" "${label}"
run_contest evaluate \
  "${CASE_NAME}" \
  "${CONTAINER_INPUT}" \
  "${CONTAINER_ROOT}/output/${CASE_NAME}/${label}" \
  "${CONTAINER_ROOT}/reports/${CASE_NAME}/${label}"

metrics="${host_reports}/metrics.json"
[[ -f "${metrics}" ]] || die "evaluation finished without metrics: ${metrics}"
printf 'Metrics: %s\n' "${metrics}"
cat "${metrics}"
