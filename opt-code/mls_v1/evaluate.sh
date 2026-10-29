#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)/common.sh"
prepare_environment
label=${1:-mls_v1}
[[ ${label} =~ ^[A-Za-z0-9._-]+$ && ${CASE_NAME} =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid label or case name"
submission="${OPEN3DBENCH_ROOT}/output/${CASE_NAME}/${label}"
report="${OPEN3DBENCH_ROOT}/reports/${CASE_NAME}/${label}"
[[ -f ${submission}/5_1_grt.odb ]] || die "missing GRT result: ${submission}/5_1_grt.odb"
mkdir -p "${report}"
run_contest evaluate "${CASE_NAME}" "${CONTAINER_INPUT}" \
  "${CONTAINER_ROOT}/output/${CASE_NAME}/${label}" \
  "${CONTAINER_ROOT}/reports/${CASE_NAME}/${label}"
python3 - "${report}/metrics.json" <<'PY'
import json
import sys
from pathlib import Path
metrics = json.loads(Path(sys.argv[1]).read_text())
print(json.dumps(metrics, indent=2))
if metrics.get("legal") is not True:
    raise SystemExit("Evaluation did not confirm legality")
PY
