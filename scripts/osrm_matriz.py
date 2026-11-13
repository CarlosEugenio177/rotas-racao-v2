"""Consulta o OSRM e gera as matrizes reais de distância (km) e tempo (h) entre depósito e pedidos.

Uso: python3 scripts/osrm_matriz.py [URL_OSRM]
  URL padrão: https://router.project-osrm.org  (servidor público de demonstração, perfil carro)
  Recomendado: OSRM local via Docker (ver README) -> http://localhost:5000

Saída: dados/matriz_km.csv, dados/matriz_h.csv, dados/pontos.csv
Ponto 0 = depósito (Teresina). Pontos fora da estrada são "encaixados" na via mais próxima;
o trecho até a via (snap) é somado como estrada de terra a 20 km/h.
"""
import csv, json, sys, time, urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
OSRM = (sys.argv[1] if len(sys.argv) > 1 else "https://router.project-osrm.org").rstrip("/")
BLOCO = 50            # coordenadas por bloco de requisição
VEL_SNAP_KMH = 20.0   # velocidade no acesso até a estrada mapeada
FATOR_SNAP = 1.3

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "rotas-racao-poc/0.2"})
    for tent in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
            if d.get("code") != "Ok":
                raise RuntimeError(d)
            return d
        except Exception as e:
            if tent == 3: raise
            print(f"  erro ({e}); tentando de novo...", file=sys.stderr)
            time.sleep(2 * (tent + 1))

def pausa():
    if "project-osrm.org" in OSRM:
        time.sleep(1.1)  # respeitar o servidor público

def main():
    cid = {c["nome"]: c for c in csv.DictReader(open(RAIZ / "dados/cidades.csv", encoding="utf-8"))}
    dep = cid["Teresina"]
    pts = [("Depósito Teresina", float(dep["lat"]), float(dep["lon"]))]
    for p in csv.DictReader(open(RAIZ / "dados/pedidos.csv", encoding="utf-8")):
        pts.append((p["nome"], float(p["lat"]), float(p["lon"])))
    n = len(pts)
    km = [[0.0] * n for _ in range(n)]
    hh = [[0.0] * n for _ in range(n)]
    snap = [0.0] * n
    nome_via = [""] * n
    blocos = [list(range(i, min(i + BLOCO, n))) for i in range(0, n, BLOCO)]
    total = len(blocos) ** 2
    k = 0
    for bs in blocos:
        for bd in blocos:
            k += 1
            idx = bs if bs == bd else bs + bd
            coords = ";".join(f"{pts[i][2]:.5f},{pts[i][1]:.5f}" for i in idx)
            src = ";".join(str(x) for x in range(len(bs)))
            dst = ";".join(str(x) for x in (range(len(bd)) if bs == bd else range(len(bs), len(bs) + len(bd))))
            url = f"{OSRM}/table/v1/driving/{coords}?sources={src}&destinations={dst}&annotations=duration,distance"
            print(f"  tabela {k}/{total} ({len(idx)} pontos)")
            d = get(url)
            for a, i in enumerate(bs):
                snap[i] = d["sources"][a]["distance"] / 1000.0
                nome_via[i] = d["sources"][a].get("name", "")
                for b, j in enumerate(bd):
                    dist, dur = d["distances"][a][b], d["durations"][a][b]
                    if dist is None or dur is None:
                        raise SystemExit(f"Sem rota entre {pts[i][0]} e {pts[j][0]}")
                    km[i][j] = dist / 1000.0
                    hh[i][j] = dur / 3600.0
            pausa()
    # soma o acesso da fazenda até a via (ida e volta conforme o trecho)
    for i in range(n):
        for j in range(n):
            if i == j: continue
            extra = (snap[i] + snap[j]) * FATOR_SNAP
            km[i][j] += extra
            hh[i][j] += extra / VEL_SNAP_KMH
    for nome, M in (("matriz_km.csv", km), ("matriz_h.csv", hh)):
        with open(RAIZ / "dados" / nome, "w", encoding="utf-8") as f:
            for row in M:
                f.write(",".join(f"{x:.4f}" for x in row) + "\n")
    with open(RAIZ / "dados/pontos.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["idx", "nome", "lat", "lon", "snap_km", "via"])
        for i, (nm, la, lo) in enumerate(pts):
            w.writerow([i, nm, la, lo, f"{snap[i]:.2f}", nome_via[i]])
    longe = [pts[i][0] for i in range(n) if snap[i] > 3]
    print(f"Matriz {n}x{n} ok via {OSRM}.")
    if longe:
        print(f"  Atenção: {len(longe)} pontos a >3 km da estrada mapeada: {', '.join(longe[:6])}...")

if __name__ == "__main__":
    main()
