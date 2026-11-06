#!/usr/bin/env bash
set -euo pipefail
algorithm_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  printf 'Usage: CASE_NAME=bp_fe MLSV2_MAX_NETS=1 %s [label]\n' "$0"
  printf 'Optional: MLSV2_CONFIG, MLSV2_THREADS (1..32), MLSV2_MAX_TRIALS\n'
  exit 0
fi
source "${algorithm_dir}/../scripts/common.sh"
label=${1:-mls_v2}
[[ ${label} =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && ${#label} -le 80 ]] || die "invalid label"
[[ ${CASE_NAME} =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "invalid case"
threads=${MLSV2_THREADS:-8}
[[ ${threads} =~ ^[0-9]+$ ]] && (( threads >= 1 && threads <= 32 )) || die "MLSV2_THREADS must be in 1..32"
prepare_environment
require_command flock
# The contest flow shares materialization and Makefile workspaces across cases.
exec 9>"${OPEN3DBENCH_ROOT}/.contest/mls_v2.lock"
flock -n 9 || die "another MLSv2 run owns the shared contest workspace"
export OPEN3DBENCH_ROOT CASE_NAME
export MLSV2_THREADS=${threads}
exec python3 -B "${algorithm_dir}/run.py" --label "${label}" \
  --config "${MLSV2_CONFIG:-${algorithm_dir}/config.json}"
