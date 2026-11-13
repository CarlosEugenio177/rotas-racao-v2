"""Gera um conjunto FIXO de fazendas fictícias perto de cidades reais do Piauí (Fase 1 do plano).

Mesma lógica do gerar_pedidos.py (cidade sorteada pelo peso, 3 a 14 km do centro), mas só os
lugares: os pedidos de cada semana são sorteados depois, por scripts/semanas.py, a partir daqui.
Assim a matriz de estradas é buscada uma vez só.

Saída: dados/semanas/fazendas.csv  (id,nome,lat,lon)
Uso: python3 scripts/gerar_fazendas.py [quantas=200] [seed=2026]
"""
import csv, math, random, sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
N = int(sys.argv[1]) if len(sys.argv) > 1 else 200
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 2026

def main():
    rng = random.Random(SEED)
    cidades = list(csv.DictReader(open(RAIZ / "dados/cidades.csv", encoding="utf-8")))
    cand = [c for c in cidades if float(c["peso"]) > 0]
    pesos = [float(c["peso"]) for c in cand]
    linhas = []
    for i in range(1, N + 1):
        c = rng.choices(cand, weights=pesos)[0]
        ang = rng.uniform(0, 2 * math.pi)
        r_km = rng.uniform(3, 14)
        lat = float(c["lat"]) + r_km / 111.0 * math.sin(ang)
        lon = float(c["lon"]) + r_km / (111.0 * math.cos(math.radians(float(c["lat"])))) * math.cos(ang)
        linhas.append(dict(id=i, nome=f"F{i:03d} {c['nome']}", lat=f"{lat:.5f}", lon=f"{lon:.5f}"))
    saida = RAIZ / "dados/semanas"
    saida.mkdir(parents=True, exist_ok=True)
    with open(saida / "fazendas.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas[0].keys()))
        w.writeheader(); w.writerows(linhas)
    print(f"{len(linhas)} fazendas em dados/semanas/fazendas.csv")

if __name__ == "__main__":
    main()
