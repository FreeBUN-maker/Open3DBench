#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OPT_CODE_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
REPO_ROOT=$(cd "${OPT_CODE_ROOT}/.." && pwd)
GRT_OVERLAY_DIR="${OPT_CODE_ROOT}/baseline"

CONTEST_IMAGE=${OPEN3DBENCH_CONTEST_IMAGE:-gaocr/3dbench-contest:20260914}
CASE_NAME=${CASE_NAME:-bp_fe}
INPUT_NAME=${OPEN3DBENCH_INPUT_NAME:-open3dbench_8cases_post_hbt_input_20260724}
CONTAINER_ROOT=/workspace/Open3DBench
CONTAINER_INPUT="${CONTAINER_ROOT}/input/${INPUT_NAME}"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

resolve_open3dbench_root() {
  # Use this checkout by default, regardless of the caller's working directory.
  # An external workspace may still hold the input data and generated results.
  OPEN3DBENCH_ROOT=${OPEN3DBENCH_ROOT:-${REPO_ROOT}}

  OPEN3DBENCH_ROOT=$(cd "${OPEN3DBENCH_ROOT}" 2>/dev/null && pwd) || die \
    "OPEN3DBENCH_ROOT does not exist: ${OPEN3DBENCH_ROOT}"

  [[ -d "${OPEN3DBENCH_ROOT}/input/${INPUT_NAME}" ]] || die \
    "contest input is missing: ${OPEN3DBENCH_ROOT}/input/${INPUT_NAME}"
  [[ -d "${GRT_OVERLAY_DIR}/openroad_src" && \
     -d "${GRT_OVERLAY_DIR}/flow_scripts" ]] || die \
    "baseline overlay is missing: ${GRT_OVERLAY_DIR}"

  mkdir -p \
    "${OPEN3DBENCH_ROOT}/.contest/home" \
    "${OPEN3DBENCH_ROOT}/output" \
    "${OPEN3DBENCH_ROOT}/reports"
}

verify_image() {
  local image_id
  image_id=$(docker image inspect --format '{{.Id}}' "${CONTEST_IMAGE}" 2>/dev/null) || die \
    "Docker image ${CONTEST_IMAGE} is unavailable; load the image from the official offline bundle"

  case "${image_id}" in
    sha256:eb877c5e1f4d94881992b55d474ab189ab3308dac3f38208c5c8b0dd7544bd98|\
    sha256:ac5b1f7476f5d05485a67a0d39e031e9e34410158d875912a0568b2b51126623)
      ;;
    *)
      if [[ ${ALLOW_UNVERIFIED_IMAGE:-0} != 1 ]]; then
        die "unexpected contest image ID ${image_id}; set ALLOW_UNVERIFIED_IMAGE=1 only if this is intentional"
      fi
      printf 'warning: using unverified contest image ID %s\n' "${image_id}" >&2
      ;;
  esac
}

prepare_environment() {
  require_command docker
  resolve_open3dbench_root
  verify_image

  DOCKER_RUN_ARGS=(
    run --rm --init
    --user "$(id -u):$(id -g)"
    --ulimit stack=-1:-1
    -e "HOME=${CONTAINER_ROOT}/.contest/home"
    -e "CONTEST_ROOT=${CONTAINER_ROOT}"
    -v "${OPEN3DBENCH_ROOT}:${CONTAINER_ROOT}"
    -v "${GRT_OVERLAY_DIR}:${CONTAINER_ROOT}/OpenROAD-GRT-HBT:ro"
    -w "${CONTAINER_ROOT}"
  )

  if [[ -t 0 && -t 1 ]]; then
    DOCKER_RUN_ARGS+=(-it)
  fi
}

run_contest() {
  local extra_args=()
  if declare -p CONTEST_EXTRA_DOCKER_ARGS >/dev/null 2>&1; then
    extra_args=("${CONTEST_EXTRA_DOCKER_ARGS[@]}")
  fi
  docker "${DOCKER_RUN_ARGS[@]}" "${extra_args[@]}" "${CONTEST_IMAGE}" "$@"
}

require_bp_fe() {
  [[ ${CASE_NAME} == bp_fe ]] || die \
    "these migrated scripts currently validate only CASE_NAME=bp_fe (got ${CASE_NAME})"
}
