#!/usr/bin/env bash
# Internal: one isolated full replay, both GRT passes, guide checks and STA.
set -euo pipefail
algorithm_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "${algorithm_dir}/../scripts/common.sh"
prepare_environment
label=$1
plan=$(realpath "$2")
config=$(realpath "$3")
[[ ${label} =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "invalid pass label"
CONTEST_EXTRA_DOCKER_ARGS=(
  --name "${MLSV2_CONTAINER_NAME}"
  -v "${algorithm_dir}:${CONTAINER_ROOT}/opt-code/mls_v2:ro"
  -v "${algorithm_dir}/../mls_v1:${CONTAINER_ROOT}/opt-code/mls_v1:ro"
  -v "${plan}:/tmp/mlsv2_plan.tcl:ro"
  -e "GRT_PREPARE_TCL=${CONTAINER_ROOT}/opt-code/mls_v2/prepare.tcl"
  -e "GRT_FEEDBACK_TCL=${CONTAINER_ROOT}/opt-code/mls_v2/export_pass.tcl"
  -e "GRT_FINAL_FEEDBACK_TCL=${CONTAINER_ROOT}/opt-code/mls_v2/snapshot.tcl"
  -e MLSV2_PLAN_TCL=/tmp/mlsv2_plan.tcl
  -e "NPROC=${MLSV2_THREADS:-8}" -e "NUM_CORES=${MLSV2_THREADS:-8}"
  -e "OMP_NUM_THREADS=${MLSV2_THREADS:-8}" -e OPENBLAS_NUM_THREADS=1
  -e VALIDATE_DIE_GUIDES=1
)
mapfile -t budgets < <(python3 - "${config}" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
for key in ('path_count', 'endpoint_path_count', 'path_point_budget', 'slack_margin_ns'):
    print(c[key])
PY
)
CONTEST_EXTRA_DOCKER_ARGS+=(
  -e "MLSV2_PATH_COUNT=${budgets[0]}" -e "MLSV2_ENDPOINT_PATH_COUNT=${budgets[1]}"
  -e "MLSV2_PATH_POINT_BUDGET=${budgets[2]}" -e "MLSV2_SLACK_MARGIN_NS=${budgets[3]}"
)
run_contest run-grt "${CASE_NAME}" "${CONTAINER_INPUT}" "${label}"
mapfile -d '' paths < <(find "${OPEN3DBENCH_ROOT}/.contest/flow-work/results" \
  -path "*/${CASE_NAME}/${label}/mls_timing.tsv" -print0)
[[ ${#paths[@]} -eq 1 ]] || die "expected one completed MLSv2 snapshot"
results=$(dirname "${paths[0]}")
submission="${OPEN3DBENCH_ROOT}/output/${CASE_NAME}/${label}"
[[ -s ${results}/.grt_finalize_complete && -s ${submission}/5_1_grt.odb ]] || die "incomplete GRT trial"
cp "${results}"/mls_* "${submission}/"
cp "${results}"/route*.guide "${submission}/"
cp -r "${results}/die_net_lists" "${submission}/"
