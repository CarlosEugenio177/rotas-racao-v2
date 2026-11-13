"""Gera uma semana de pedidos fictícios de fazendas perto de cidades reais do Piauí.

Saída: dados/pedidos.csv  (id,nome,lat,lon,demanda_t,dia,u)
  dia = dia em que o pedido chega (0..DIAS-1)
  u   = número aleatório usado pelo solver para sortear quais pedidos são flexíveis
"""
import csv, math, random, sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DIAS = 5
PEDIDOS_POR_DIA = (9, 13)
SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 7

def main():
    rng = random.Random(SEED)
    cidades = list(csv.DictReader(open(RAIZ / "dados/cidades.csv", encoding="utf-8")))
    cand = [c for c in cidades if float(c["peso"]) > 0]
    pesos = [float(c["peso"]) for c in cand]
    linhas = []
    for dia in range(DIAS):
        for _ in range(rng.randint(*PEDIDOS_POR_DIA)):
            c = rng.choices(cand, weights=pesos)[0]
            ang = rng.uniform(0, 2 * math.pi)
            r_km = rng.uniform(3, 14)
            lat = float(c["lat"]) + r_km / 111.0 * math.sin(ang)
            lon = float(c["lon"]) + r_km / (111.0 * math.cos(math.radians(float(c["lat"])))) * math.cos(ang)
            dem = rng.choice([1, 1.5, 2, 2, 2.5, 3, 3, 3.5, 4, 5, 6])
            i = len(linhas) + 1
            linhas.append(dict(id=i, nome=f"P{i:02d} {c['nome']}", lat=f"{lat:.5f}", lon=f"{lon:.5f}",
                               demanda_t=dem, dia=dia, u=f"{rng.random():.4f}"))
    with open(RAIZ / "dados/pedidos.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas[0].keys()))
        w.writeheader(); w.writerows(linhas)
    print(f"{len(linhas)} pedidos em {DIAS} dias, {sum(float(l['demanda_t']) for l in linhas):.1f} t")

if __name__ == "__main__":
    main()
