#!/usr/bin/env bash
# Teste offline com OSRM FALSO (distâncias inventadas). Só para validar que tudo roda.
set -euo pipefail
cd "$(dirname "$0")"
python3 scripts/mock_osrm.py 5999 & PID=$!
trap "kill $PID 2>/dev/null || true" EXIT
sleep 1
./run.sh http://127.0.0.1:5999
