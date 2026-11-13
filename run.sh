#!/usr/bin/env bash
# Pipeline completo: pedidos -> matriz OSRM -> solver Rust -> mapa
# Uso: ./run.sh [URL_OSRM]   (padrão: servidor público https://router.project-osrm.org)
set -euo pipefail
OSRM="${1:-https://router.project-osrm.org}"
cd "$(dirname "$0")"
echo "== 1/4 gerando pedidos fictícios";       python3 scripts/gerar_pedidos.py
echo "== 2/4 matriz de distâncias reais ($OSRM)"; python3 scripts/osrm_matriz.py "$OSRM"
echo "== 3/4 otimizando";                      (cd solver && cargo run --release -q -- ..)
echo "== 4/4 desenhando rotas";                python3 scripts/mapa.py "$OSRM"
echo "Pronto: abra saida/mapa.html no navegador."
