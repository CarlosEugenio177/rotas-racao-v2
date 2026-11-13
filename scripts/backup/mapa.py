"""Busca a geometria real de cada viagem no OSRM e gera saida/mapa.html (Leaflet + OpenStreetMap).

Uso: python3 scripts/mapa.py [URL_OSRM]
"""
import csv, json, sys, time, urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
OSRM = (sys.argv[1] if len(sys.argv) > 1 else "https://router.project-osrm.org").rstrip("/")

def rota(coords):
    url = f"{OSRM}/route/v1/driving/" + ";".join(f"{lo:.5f},{la:.5f}" for la, lo in coords) + "?overview=simplified&geometries=geojson"
    req = urllib.request.Request(url, headers={"User-Agent": "rotas-racao-poc/0.2"})
    for tent in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
            if "project-osrm.org" in OSRM: time.sleep(1.1)
            return [[la, lo] for lo, la in d["routes"][0]["geometry"]["coordinates"]]
        except Exception as e:
            if tent == 3:
                print(f"  sem geometria ({e}); desenhando reta", file=sys.stderr)
                return [list(c) for c in coords]
            time.sleep(2 * (tent + 1))

def main():
    res = json.load(open(RAIZ / "saida/resultado.json", encoding="utf-8"))
    pts = list(csv.DictReader(open(RAIZ / "dados/pontos.csv", encoding="utf-8")))
    ped = list(csv.DictReader(open(RAIZ / "dados/pedidos.csv", encoding="utf-8")))
    coord = [(float(p["lat"]), float(p["lon"])) for p in pts]
    cache = {}
    total = sum(len(v) for pol in res["politicas"] for v in pol["dias"])
    k = 0
    for pol in res["politicas"]:
        for dia in pol["dias"]:
            for v in dia:
                k += 1
                chave = tuple(v["pedidos"])
                if chave not in cache:
                    print(f"  geometria {k}/{total}", end="\r")
                    cache[chave] = rota([coord[0]] + [coord[i] for i in v["pedidos"]] + [coord[0]])
                v["geo"] = cache[chave]
    print()
    pontos = [{"nome": p["nome"], "lat": coord[i][0], "lon": coord[i][1], "snap": float(p["snap_km"]),
               "dem": float(ped[i - 1]["demanda_t"]) if i else 0, "dia": int(ped[i - 1]["dia"]) if i else -1}
              for i, p in enumerate(pts)]
    html = (RAIZ / "scripts/mapa_template.html").read_text(encoding="utf-8")
    html = html.replace("/*DADOS*/null", json.dumps({"res": res, "pontos": pontos, "osrm": OSRM}, ensure_ascii=False))
    (RAIZ / "saida/mapa.html").write_text(html, encoding="utf-8")
    print("Mapa salvo em saida/mapa.html")

if __name__ == "__main__":
    main()
