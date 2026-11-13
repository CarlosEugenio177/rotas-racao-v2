"""Matrizes de CAMINHÃO a partir dos dois roteadores locais (scripts/osrm_local.sh).

Para cada par de pontos consulta a rota nos dois perfis:
  porta 5001  caminho mais rápido      -> matriz_km.csv,     matriz_h.csv,     matriz_l.csv
  porta 5002  caminho de menor consumo -> matriz_km_eco.csv, matriz_h_eco.csv, matriz_l_eco.csv
O tempo já é de caminhão (não há mais o fator 1,25). Os litros (caminhão vazio) são somados
TRECHO A TRECHO: cada pedacinho da rota entra com a velocidade do caminhão naquele pedaço,
  litros/km = base * (1 + extra * max(0, (vref - v) / vref))        (dados/premissas.toml)
em vez de usar a velocidade média do par inteiro. O acesso da fazenda até a via mapeada (snap)
entra como estrada de terra a 20 km/h, como no osrm_matriz.py.

Uso: python3 scripts/matriz_caminhao.py [ARQUIVO_DE_PONTOS] [PASTA_DE_SAIDA]
     (padrão: dados/pedidos.csv e dados/)
"""
import csv, json, sys, tomllib, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ARQ_PONTOS = Path(sys.argv[1]) if len(sys.argv) > 1 else RAIZ / "dados/pedidos.csv"
SAIDA = Path(sys.argv[2]) if len(sys.argv) > 2 else RAIZ / "dados"
SERVIDORES = {"": "http://localhost:5001", "_eco": "http://localhost:5002"}
VEL_SNAP_KMH, FATOR_SNAP = 20.0, 1.3

with open(RAIZ / "dados/premissas.toml", "rb") as f:
    _t = tomllib.load(f)
BASE, EXTRA, VREF = _t["consumo"]["consumo_base_l_km"], _t["consumo"]["consumo_lento_extra"], _t["perfil_caminhao"]["vel_ref_caminhao_kmh"]


def consumo_l_km(v_kmh):
    return BASE * (1 + EXTRA * max(0.0, (VREF - v_kmh) / VREF))


def rota(base, a, b):
    url = f"{base}/route/v1/driving/{a[2]:.5f},{a[1]:.5f};{b[2]:.5f},{b[1]:.5f}?overview=false&annotations=distance,duration"
    for tent in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                d = json.load(r)
            break
        except Exception:
            if tent == 2: raise
    if d.get("code") != "Ok":
        raise SystemExit(f"Sem rota de caminhão entre {a[0]} e {b[0]}: {d.get('code')}")
    r = d["routes"][0]
    an = r["legs"][0]["annotation"]
    litros = 0.0
    for dist, dur in zip(an["distance"], an["duration"]):
        v = dist / dur * 3.6 if dur > 0 else VREF
        litros += dist / 1000.0 * consumo_l_km(v)
    snap = (d["waypoints"][0]["distance"] + d["waypoints"][1]["distance"]) / 1000.0 * FATOR_SNAP
    return {"km": r["distance"] / 1000.0 + snap, "h": r["duration"] / 3600.0 + snap / VEL_SNAP_KMH,
            "l": litros + snap * consumo_l_km(VEL_SNAP_KMH), "peso": r["weight"],
            "snap": [d["waypoints"][0]["distance"] / 1000.0, d["waypoints"][0].get("name", "")]}


def main():
    cid = {c["nome"]: c for c in csv.DictReader(open(RAIZ / "dados/cidades.csv", encoding="utf-8"))}
    dep = cid["Teresina"]
    pts = [("Depósito Teresina", float(dep["lat"]), float(dep["lon"]))]
    for p in csv.DictReader(open(ARQ_PONTOS, encoding="utf-8")):
        pts.append((p["nome"], float(p["lat"]), float(p["lon"])))
    n = len(pts)
    pares = [(i, j) for i in range(n) for j in range(n) if i != j]
    res = {}
    for suf, base in SERVIDORES.items():
        with ThreadPoolExecutor(max_workers=8) as ex:
            rs = list(ex.map(lambda ij: rota(base, pts[ij[0]], pts[ij[1]]), pares))
        res[suf] = dict(zip(pares, rs))
        for campo, nome in (("km", "matriz_km"), ("h", "matriz_h"), ("l", "matriz_l")):
            with open(SAIDA / f"{nome}{suf}.csv", "w", encoding="utf-8") as f:
                for i in range(n):
                    f.write(",".join("0.0000" if i == j else f"{res[suf][i, j][campo]:.4f}" for j in range(n)) + "\n")
        print(f"  {len(pares)} rotas no perfil {'econômico' if suf else 'mais rápido'} ({base})")
    with open(SAIDA / "pontos.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["idx", "nome", "lat", "lon", "snap_km", "via"])
        for i, (nm, la, lo) in enumerate(pts):
            s = res[""][i, (i + 1) % n]["snap"]
            w.writerow([i, nm, la, lo, f"{s[0]:.2f}", s[1]])
    # quantos pares têm um caminho econômico de fato diferente do mais rápido
    rap, eco = res[""], res["_eco"]
    dif = [(ij, rap[ij], eco[ij]) for ij in pares if eco[ij]["l"] < rap[ij]["l"] - 0.05 and abs(eco[ij]["km"] - rap[ij]["km"]) > 0.05]
    desv = max(abs(eco[ij]["peso"] / 1000.0 - (eco[ij]["l"] - (eco[ij]["km"] - 0) * 0)) / max(eco[ij]["l"], 1e-9) for ij in pares) if pares else 0
    resumo = {"pontos": n, "pares": len(pares), "pares_com_caminho_economico_diferente": len(dif),
              "litros_poupados_medio": round(sum(r["l"] - e["l"] for _, r, e in dif) / len(dif), 2) if dif else 0,
              "minutos_a_mais_medio": round(sum(e["h"] - r["h"] for _, r, e in dif) / len(dif) * 60, 1) if dif else 0,
              "litros_poupados_max": round(max((r["l"] - e["l"] for _, r, e in dif), default=0), 2)}
    json.dump(resumo, open(SAIDA / "matriz_caminhao.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"Matrizes de caminhão {n}x{n} em {SAIDA}.")
    print(f"  Em {len(dif)} de {len(pares)} pares o caminho de menor consumo é outro: poupa em média {resumo['litros_poupados_medio']} L "
          f"(máx. {resumo['litros_poupados_max']} L) e leva {resumo['minutos_a_mais_medio']} min a mais.")


if __name__ == "__main__":
    main()
