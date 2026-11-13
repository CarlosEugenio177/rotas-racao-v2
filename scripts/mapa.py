"""Busca a geometria real de cada viagem no OSRM e gera saida/mapa.html (Leaflet + OpenStreetMap).

Uso: python3 scripts/mapa.py [URL_OSRM]
"""
import csv, json, sys, time, urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
# Sem argumento: o roteador local de caminhão quando as matrizes são de caminhão (scripts/osrm_local.sh), senão o OSRM público.
PADRAO = "http://localhost:5001" if (RAIZ / "dados/matriz_l.csv").exists() else "https://router.project-osrm.org"
OSRM = (sys.argv[1] if len(sys.argv) > 1 else PADRAO).rstrip("/")

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
    arq_cache = RAIZ / "saida/geometrias.json"
    todos = json.load(open(arq_cache, encoding="utf-8")) if arq_cache.exists() else {}
    cache = {tuple(json.loads(k)): g for k, g in todos.get(OSRM, {}).items()}
    total = sum(len(v) for pol in res["politicas"] for v in pol["dias"])
    k = 0
    # tempo x combustível (scripts/pareto.py grava saida/pareto.json): os 3 planos com nome também vão para o mapa
    arq_par = RAIZ / "saida/pareto.json"
    pareto = json.load(open(arq_par, encoding="utf-8")) if arq_par.exists() else None
    if pareto and "semanas" in pareto:
        pareto["semanas"].pop("detalhe", None)
    planos = list(pareto["planos"].values()) if pareto else []
    total += sum(len(v) for pol in planos for v in pol["dias"])
    for pol in res["politicas"] + ([res["base_regiao"]] if "base_regiao" in res else []) + planos:
        for dia in pol["dias"]:
            for v in dia:
                k += 1
                chave = tuple(v["pedidos"])
                if chave not in cache:
                    print(f"  geometria {k}/{total}", end="\r")
                    cache[chave] = rota([coord[0]] + [coord[i] for i in v["pedidos"]] + [coord[0]])
                v["geo"] = cache[chave]
    print()
    todos[OSRM] = {json.dumps(list(k)): g for k, g in cache.items()}
    arq_cache.write_text(json.dumps(todos), encoding="utf-8")
    pontos = [{"nome": p["nome"], "lat": coord[i][0], "lon": coord[i][1], "snap": float(p["snap_km"]),
               "dem": float(ped[i - 1]["demanda_t"]) if i else 0, "dia": int(ped[i - 1]["dia"]) if i else -1}
              for i, p in enumerate(pts)]
    # modelo exato x heurística (scripts/exato.py grava saida/exato.json, mesclado por tamanho n)
    arq_ex = RAIZ / "saida/exato.json"
    exato = [{k: l[k] for k in ("n", "exato", "tempo_exato", "otimo_provado", "heuristica", "tempo_heur", "gap_pct", "lp", "binarias", "restricoes")}
             for l in (json.load(open(arq_ex, encoding="utf-8")) if arq_ex.exists() else [])]
    # outros preços de diesel já simulados (saida/resultado_diesel_*.json)
    diesel = []
    for arq in sorted((RAIZ / "saida").glob("resultado_diesel_*.json")):
        r = json.load(open(arq, encoding="utf-8"))
        enxuga = lambda p: {k: p[k] for k in ("nome", "flex", "custo", "km", "litros")}
        diesel.append({"diesel": r["parametros"]["diesel_rs_l"], "politicas": [enxuga(p) for p in r["politicas"]],
                       "base_regiao": enxuga(r["base_regiao"]) if "base_regiao" in r else None})
    # várias semanas simuladas (scripts/semanas.py grava saida/semanas.json)
    arq_sem = RAIZ / "saida/semanas.json"
    semanas = None
    if arq_sem.exists():
        sm = json.load(open(arq_sem, encoding="utf-8"))
        # a página mede a economia contra a linha de base forte; a outra aparece só como comparação
        forte = sm["base_forte"]
        outra = "simples" if forte == "regiao" else "regiao"
        eco = lambda w, b: [round((1 - q["custo"] / w["bases"][b]["custo"]) * 100, 2) for q in w["politicas"]]
        semanas = {k: sm[k] for k in ("n", "fazendas", "volumes", "frota_livre", "base_forte", "regiao_ganha_em", "custo_base")}
        semanas["resumo"], semanas["resumo_outra"] = sm["resumo"][forte], sm["resumo"][outra]
        semanas["semanas"] = [{"seed": w["seed"], "volume": w["volume"], "pedidos": w["pedidos"], "frota_livre": w["frota_livre"],
                               "custo_hoje": w["bases"][forte]["custo"], "custo_outra": w["bases"][outra]["custo"],
                               "cam": [max(w["bases"][forte]["caminhoes_dia"]), max(w["politicas"][0]["caminhoes_dia"])],
                               "eco": eco(w, forte), "eco_outra": eco(w, outra)} for w in sm["semanas"]]
    # ótimo provado com frota (scripts/exato_frota.py grava saida/exato_frota.json e saida/exato_semanas.json)
    prova = None
    arq_ef, arq_es = RAIZ / "saida/exato_frota.json", RAIZ / "saida/exato_semanas.json"
    if arq_ef.exists():
        ef = json.load(open(arq_ef, encoding="utf-8"))
        sem_plano = lambda l: {k: v for k, v in l.items() if k != "plano_exato"}
        prova = {"dias": [sem_plano(l) for l in ef["mesmo_dia"]], "semana": ef["semana"],
                 "flex": sem_plano(ef["flexivel_pequena"]), "conferencia": ef["conferencia"], "semanas": None}
        if arq_es.exists():
            es = json.load(open(arq_es, encoding="utf-8"))
            prova["semanas"] = {"mesmo_dia": es["mesmo_dia"], "flexivel": es["flexivel"]}
    # perfil de caminhão: premissas do perfil e comparação com as matrizes antigas de carro
    import tomllib
    with open(RAIZ / "dados/premissas.toml", "rb") as f:
        perfil = tomllib.load(f).get("perfil_caminhao")
    arq_cmp = RAIZ / "saida/comparacao_caminhao.json"
    comparacao = json.load(open(arq_cmp, encoding="utf-8")) if arq_cmp.exists() else None
    html = (RAIZ / "scripts/mapa_template.html").read_text(encoding="utf-8")
    html = html.replace("/*DADOS*/null", json.dumps({"res": res, "pontos": pontos, "osrm": OSRM,
                                                    "exato": sorted(exato, key=lambda l: l["n"]), "diesel": diesel, "semanas": semanas, "pareto": pareto, "prova": prova, "perfil_caminhao": perfil, "comparacao": comparacao}, ensure_ascii=False))
    (RAIZ / "saida/mapa.html").write_text(html, encoding="utf-8")
    print("Mapa salvo em saida/mapa.html")

if __name__ == "__main__":
    main()
