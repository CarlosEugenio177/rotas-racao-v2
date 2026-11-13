"""Fase 1 do plano: a economia é robusta? Roda todas as políticas em várias semanas sorteadas.

As semanas saem de um conjunto fixo de fazendas (dados/semanas/fazendas.csv, com a matriz de
estradas já buscada em dados/semanas/matriz_*.csv). Cada semana é definida só pela seed:
  - volume (fraca / normal / forte) pela posição da seed, ver VOLUME_DA_SEED;
  - em cada dia, sorteia quantos pedidos chegam (faixa do volume) e de quais fazendas
    (sem repetir fazenda na mesma semana);
  - toneladas e "u" (quem é flexível) como no gerar_pedidos.py.

O solver roda numa pasta temporária por semana, com as premissas de dados/premissas.toml.
Se o "jeito de hoje" (ou o plano do mesmo dia) não couber na frota em algum dia, a semana é
rodada de novo com frota livre (FROTA=99, mesmo custo por caminhão) e fica marcada.

Saída: saida/semanas.json
Uso: python3 scripts/semanas.py [primeira_seed=1] [ultima_seed=20]
"""
import csv, json, os, random, shutil, statistics, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
BASE = RAIZ / "dados/semanas"
DIAS = 5
# Pedidos por dia em cada tipo de semana. PREMISSA: "normal" é a faixa da instância de
# referência (gerar_pedidos.py); "fraca" e "forte" são chutes para variar o volume.
VOLUMES = {"fraca": (6, 9), "normal": (9, 13), "forte": (12, 16)}
VOLUME_DA_SEED = ["normal", "fraca", "normal", "forte"]  # seeds 1,2,3,4, 5,6,...: 10 normais, 5 fracas, 5 fortes em 20
DEMANDAS_T = [1, 1.5, 2, 2, 2.5, 3, 3, 3.5, 4, 5, 6]     # igual ao gerar_pedidos.py


def ler_matriz(arq):
    return [[float(x) for x in l.split(",")] for l in arq.read_text().splitlines() if l.strip()]


def sortear(seed, fazendas):
    """Devolve (volume, pedidos); cada pedido = dict(faz=índice na matriz, nome, lat, lon, dem, dia, u)."""
    volume = VOLUME_DA_SEED[(seed - 1) % len(VOLUME_DA_SEED)]
    rng = random.Random(seed)
    livres = list(range(1, len(fazendas) + 1))
    pedidos = []
    for dia in range(DIAS):
        for _ in range(rng.randint(*VOLUMES[volume])):
            faz = livres.pop(rng.randrange(len(livres)))
            f = fazendas[faz - 1]
            pedidos.append(dict(faz=faz, nome=f["nome"], lat=f["lat"], lon=f["lon"],
                                dem=rng.choice(DEMANDAS_T), dia=dia, u=f"{rng.random():.4f}"))
    return volume, pedidos


def rodar_solver(pedidos, km, h, frota_livre):
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "dados").mkdir()
        shutil.copy(RAIZ / "dados/premissas.toml", tmp / "dados/premissas.toml")
        with open(tmp / "dados/pedidos.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "nome", "lat", "lon", "demanda_t", "dia", "u"])
            for k, p in enumerate(pedidos, 1):
                w.writerow([k, p["nome"], p["lat"], p["lon"], p["dem"], p["dia"], p["u"]])
        idx = [0] + [p["faz"] for p in pedidos]
        for nome, M in (("matriz_km.csv", km), ("matriz_h.csv", h)):
            with open(tmp / "dados" / nome, "w") as f:
                for i in idx:
                    f.write(",".join(f"{M[i][j]:.4f}" for j in idx) + "\n")
        env = {k: v for k, v in os.environ.items() if k not in ("FROTA", "CAMINHAO_RS_DIA")}
        if frota_livre:
            env["FROTA"] = "99"
        r = subprocess.run([str(RAIZ / "solver/target/release/solver"), str(tmp)], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            return None, r.stderr
        return json.load(open(tmp / "saida/resultado.json", encoding="utf-8")), ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def uma_semana(seed, fazendas, km, h):
    volume, pedidos = sortear(seed, fazendas)
    res, erro = rodar_solver(pedidos, km, h, frota_livre=False)
    frota_livre, motivo = False, ""
    if res is None:
        if "frota estourada" not in erro:
            raise RuntimeError(f"semana {seed}: o solver falhou:\n{erro}")
        motivo = erro.strip().splitlines()[1] if len(erro.strip().splitlines()) > 1 else erro.strip()
        res, erro = rodar_solver(pedidos, km, h, frota_livre=True)
        if res is None:
            raise RuntimeError(f"semana {seed}: falhou mesmo com frota livre:\n{erro}")
        frota_livre = True
    a = res["politicas"][0]
    pols = [{"nome": p["nome"], "flex": p["flex"], "custo": p["custo"], "km": p["km"], "litros": p["litros"],
             "horas": p["horas"], "viagens": p["viagens"], "mesmo_dia_pct": p["mesmo_dia_pct"],
             "caminhoes_dia": p["caminhoes_dia"], "economia_pct": round((1 - p["custo"] / a["custo"]) * 100, 2)}
            for p in res["politicas"]]
    return {"seed": seed, "volume": volume, "pedidos": len(pedidos), "fora": len(res["fora"]),
            "toneladas": sum(p["dem"] for p in pedidos), "frota_livre": frota_livre, "motivo_frota": motivo,
            "frota": res["parametros"]["frota"], "politicas": pols}


def estat(xs):
    return {"media": round(statistics.mean(xs), 2), "min": round(min(xs), 2), "max": round(max(xs), 2),
            "desvio": round(statistics.stdev(xs), 2) if len(xs) > 1 else 0.0}


def main():
    s0 = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    s1 = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    fazendas = list(csv.DictReader(open(BASE / "fazendas.csv", encoding="utf-8")))
    km, h = ler_matriz(BASE / "matriz_km.csv"), ler_matriz(BASE / "matriz_h.csv")
    assert len(km) == len(fazendas) + 1, "matriz e fazendas com tamanhos diferentes: rode osrm_matriz.py de novo"
    subprocess.run(["cargo", "build", "--release", "-q"], cwd=RAIZ / "solver", check=True)
    with ThreadPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1)) as ex:
        semanas = list(ex.map(lambda s: uma_semana(s, fazendas, km, h), range(s0, s1 + 1)))

    nomes = [p["nome"] for p in semanas[0]["politicas"]]
    resumo = []
    for k, nome in enumerate(nomes):
        if k == 0:
            continue
        resumo.append({"nome": nome, "flex": semanas[0]["politicas"][k]["flex"],
                       "economia_pct": estat([s["politicas"][k]["economia_pct"] for s in semanas]),
                       "economia_rs": estat([s["politicas"][0]["custo"] - s["politicas"][k]["custo"] for s in semanas]),
                       "km": estat([s["politicas"][0]["km"] - s["politicas"][k]["km"] for s in semanas]),
                       "litros": estat([s["politicas"][0]["litros"] - s["politicas"][k]["litros"] for s in semanas]),
                       "horas": estat([s["politicas"][0]["horas"] - s["politicas"][k]["horas"] for s in semanas]),
                       "por_volume": {v: estat([s["politicas"][k]["economia_pct"] for s in semanas if s["volume"] == v])
                                      for v in VOLUMES if any(s["volume"] == v for s in semanas)}})
    saida = {"n": len(semanas), "fazendas": len(fazendas), "volumes": {k: list(v) for k, v in VOLUMES.items()},
             "frota_livre": sum(s["frota_livre"] for s in semanas), "semanas": semanas, "resumo": resumo}
    (RAIZ / "saida").mkdir(exist_ok=True)
    json.dump(saida, open(RAIZ / "saida/semanas.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"{len(semanas)} semanas de {len(fazendas)} fazendas\n")
    print(f"{'seed':>4} {'volume':<7} {'ped':>4} {'fora':>4} {'t':>6} {'manual R$':>10} " + " ".join(f"{'B' if p['flex']==0 else str(round(p['flex']*100))+'%':>7}" for p in semanas[0]["politicas"][1:]) + "  cam/dia A | B")
    for s in semanas:
        a, b = s["politicas"][0], s["politicas"][1]
        print(f"{s['seed']:>4} {s['volume']:<7} {s['pedidos']:>4} {s['fora']:>4} {s['toneladas']:>6.1f} {a['custo']:>10.0f} "
              + " ".join(f"{p['economia_pct']:>6.1f}%" for p in s["politicas"][1:])
              + f"  {max(a['caminhoes_dia'])} | {max(b['caminhoes_dia'])}" + ("  FROTA LIVRE" if s["frota_livre"] else ""))
    print()
    for r in resumo:
        e = r["economia_pct"]
        print(f"{r['nome']:<40} economia média {e['media']:>5.1f}%  (mín {e['min']:.1f}%, máx {e['max']:.1f}%, desvio {e['desvio']:.1f})  "
              + "  ".join(f"{v}: {x['media']:.1f}%" for v, x in r["por_volume"].items()))
    print(f"\nSemanas em que o plano não coube na frota e rodou com frota livre: {saida['frota_livre']} de {len(semanas)}")
    print("Detalhes em saida/semanas.json")


if __name__ == "__main__":
    main()
