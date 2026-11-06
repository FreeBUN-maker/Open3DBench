#!/usr/bin/env bash
set -euo pipefail
algorithm_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec bash "${algorithm_dir}/../mls_v1/evaluate.sh" "${1:-mls_v2}"
