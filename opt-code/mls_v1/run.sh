#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)/common.sh"

prepare_environment
label=${1:-mls_v1}
[[ ${label} =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid run label: ${label}"
[[ ${CASE_NAME} =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid case name: ${CASE_NAME}"
algorithm_dir="${OPT_CODE_ROOT}/mls_v1"
config=${MLSV1_CONFIG:-${algorithm_dir}/config.json}
config=$(realpath "${config}")
[[ -f ${config} ]] || die "missing MLSv1 configuration: ${config}"
# Validate configuration before starting the expensive routing flow.
python3 - "${algorithm_dir}" "${config}" "${MLSV1_MAX_NETS:-}" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from planner import load_config
overrides = [] if not sys.argv[3] else [("max_nets", int(sys.argv[3]))]
load_config(Path(sys.argv[2]), overrides)
PY
threads=${MLSV1_THREADS:-8}
[[ ${threads} =~ ^[0-9]+$ ]] && (( threads >= 1 && threads <= 32 )) || die "MLSV1_THREADS must be in 1..32"
CONTEST_EXTRA_DOCKER_ARGS=(
  -v "${algorithm_dir}:${CONTAINER_ROOT}/opt-code/mls_v1:ro"
  -v "${config}:/tmp/mlsv1_config.json:ro"
  -e "GRT_PREPARE_TCL=${CONTAINER_ROOT}/opt-code/mls_v1/prepare.tcl"
  -e MLSV1_CONFIG=/tmp/mlsv1_config.json
  -e "NPROC=${threads}" -e "NUM_CORES=${threads}" -e "OMP_NUM_THREADS=${threads}"
  -e OPENBLAS_NUM_THREADS=1
)
if [[ -n ${MLSV1_MAX_NETS:-} ]]; then
  CONTEST_EXTRA_DOCKER_ARGS+=(-e "MLSV1_MAX_NETS=${MLSV1_MAX_NETS}")
fi
run_contest run-grt "${CASE_NAME}" "${CONTAINER_INPUT}" "${label}"

submission="${OPEN3DBENCH_ROOT}/output/${CASE_NAME}/${label}"
# The contest wrapper copies ODB/guides only. Preserve the algorithm evidence.
mapfile -d '' plan_paths < <(find "${OPEN3DBENCH_ROOT}/.contest/flow-work/results" \
  -path "*/${CASE_NAME}/${label}/mls_plan.json" -print0)
[[ ${#plan_paths[@]} -eq 1 ]] || die "expected one planner report for ${CASE_NAME}/${label}"
results=$(dirname "${plan_paths[0]}")
cp "${results}/mls_plan.json" "${submission}/"
if [[ -f ${results}/mls_commits.tsv ]]; then
  cp "${results}/mls_commits.tsv" "${submission}/"
else
  rm -f "${submission}/mls_commits.tsv"
fi
printf 'GRT output: %s/5_1_grt.odb\n' "${submission}"
