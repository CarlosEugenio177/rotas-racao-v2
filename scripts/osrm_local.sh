#!/usr/bin/env bash
# Sobe dois roteadores OSRM locais com perfil de CAMINHÃO (Fase 5 do plano):
#   porta 5001  perfis/caminhao.lua      caminho mais rápido
#   porta 5002  perfis/caminhao_eco.lua  caminho de menor consumo
# Usa o mapa do Nordeste da Geofabrik recortado para a área das entregas (Piauí e vizinhança).
# Precisa de Docker (~2 GB de RAM livres e ~1,5 GB de disco em osrm/, que fica fora do git).
#
# Uso: scripts/osrm_local.sh [preparar|subir|parar]     (sem argumento: preparar e subir)
set -euo pipefail
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
OSRM_IMG=ghcr.io/project-osrm/osrm-backend:latest
OSMIUM_IMG=iboates/osmium:latest
CAIXA="-44.6,-8.3,-40.2,-2.2"   # oeste,sul,leste,norte: de Parnaíba a Picos/Oeiras, com folga
# No WSL sem integração do Docker Desktop, o cliente é o docker.exe do Windows e os caminhos têm que ser do Windows.
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  DOCKER=docker; vol() { echo "$1"; }
else
  DOCKER="$(ls /mnt/c/Users/*/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe "/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe" 2>/dev/null | head -1 || true)"
  [ -n "$DOCKER" ] || { echo "Docker não encontrado"; exit 1; }
  vol() { wslpath -w "$1"; }
fi
mkdir -p "$RAIZ/osrm"

preparar() {
  [ -f "$RAIZ/osrm/nordeste.osm.pbf" ] || curl -L -o "$RAIZ/osrm/nordeste.osm.pbf" https://download.geofabrik.de/south-america/brazil/nordeste-latest.osm.pbf
  [ -f "$RAIZ/perfis/car_original.lua" ] || "$DOCKER" run --rm $OSRM_IMG cat /opt/car.lua > "$RAIZ/perfis/car_original.lua"
  python3 "$RAIZ/scripts/perfil_caminhao.py"
  [ -f "$RAIZ/osrm/recorte.osm.pbf" ] || "$DOCKER" run --rm -v "$(vol "$RAIZ/osrm"):/data" $OSMIUM_IMG extract -b "$CAIXA" /data/nordeste.osm.pbf -o /data/recorte.osm.pbf --overwrite
  for p in caminhao caminhao_eco; do
    mkdir -p "$RAIZ/osrm/$p"
    cp "$RAIZ/osrm/recorte.osm.pbf" "$RAIZ/osrm/$p/mapa.osm.pbf"
    cp "$RAIZ/perfis/$p.lua" "$RAIZ/osrm/$p/perfil.lua"
    # o perfil precisa estar em /opt para achar as bibliotecas lib/ da imagem
    "$DOCKER" run --rm -v "$(vol "$RAIZ/osrm/$p"):/data" $OSRM_IMG sh -c "cp /data/perfil.lua /opt/$p.lua && osrm-extract -p /opt/$p.lua /data/mapa.osm.pbf && osrm-partition /data/mapa.osrm && osrm-customize /data/mapa.osrm"
    rm -f "$RAIZ/osrm/$p/mapa.osm.pbf"
  done
}

parar() { "$DOCKER" rm -f osrm-caminhao osrm-caminhao_eco >/dev/null 2>&1 || true; }

subir() {
  parar
  porta=5001
  for p in caminhao caminhao_eco; do
    "$DOCKER" run -d --name "osrm-$p" -p "$porta:5000" -v "$(vol "$RAIZ/osrm/$p"):/data" $OSRM_IMG osrm-routed --algorithm mld --max-table-size 2000 /data/mapa.osrm >/dev/null
    echo "osrm-$p na porta $porta"
    porta=$((porta + 1))
  done
}

case "${1:-tudo}" in
  preparar) preparar ;;
  subir) subir ;;
  parar) parar ;;
  tudo) preparar; subir ;;
  *) echo "uso: $0 [preparar|subir|parar]"; exit 1 ;;
esac
