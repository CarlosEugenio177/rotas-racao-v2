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


def rodar_solver(pedidos, km, h, frota_livre, env_extra=None):
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
        env.update(env_extra or {})
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
    campos = ("nome", "flex", "custo", "km", "litros", "horas", "viagens", "mesmo_dia_pct", "caminhoes_dia")
    pols = [{k: p[k] for k in campos} for p in res["politicas"]]
    # duas linhas de base do "jeito de hoje": manual simples (politicas[0]) e manual por região
    bases = {"simples": pols[0], "regiao": {k: res["base_regiao"][k] for k in campos}}
    return {"seed": seed, "volume": volume, "pedidos": len(pedidos), "fora": len(res["fora"]),
            "toneladas": sum(p["dem"] for p in pedidos), "frota_livre": frota_livre, "motivo_frota": motivo,
            "frota": res["parametros"]["frota"], "bases": bases, "politicas": pols[1:]}


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

    eco = lambda s, b, k: (1 - s["politicas"][k]["custo"] / s["bases"][b]["custo"]) * 100
    resumo = {}
    for b in ("simples", "regiao"):
        resumo[b] = []
        for k, pol in enumerate(semanas[0]["politicas"]):
            dif = lambda campo: estat([s["bases"][b][campo] - s["politicas"][k][campo] for s in semanas])
            resumo[b].append({"nome": pol["nome"], "flex": pol["flex"],
                              "economia_pct": estat([eco(s, b, k) for s in semanas]),
                              "economia_rs": dif("custo"), "km": dif("km"), "litros": dif("litros"), "horas": dif("horas"),
                              "por_volume": {v: estat([eco(s, b, k) for s in semanas if s["volume"] == v])
                                             for v in VOLUMES if any(s["volume"] == v for s in semanas)}})
    custo_base = {b: estat([s["bases"][b]["custo"] for s in semanas]) for b in ("simples", "regiao")}
    # a linha de base "forte" é a mais barata na média: é contra ela que a página mede a economia
    forte = min(custo_base, key=lambda b: custo_base[b]["media"])
    saida = {"n": len(semanas), "fazendas": len(fazendas), "volumes": {k: list(v) for k, v in VOLUMES.items()},
             "frota_livre": sum(s["frota_livre"] for s in semanas), "base_forte": forte, "custo_base": custo_base,
             "regiao_ganha_em": sum(s["bases"]["regiao"]["custo"] < s["bases"]["simples"]["custo"] for s in semanas),
             "semanas": semanas, "resumo": resumo}
    (RAIZ / "saida").mkdir(exist_ok=True)
    json.dump(saida, open(RAIZ / "saida/semanas.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"{len(semanas)} semanas de {len(fazendas)} fazendas. Economia do plano do mesmo dia (B) contra cada jeito de hoje:\n")
    print(f"{'seed':>4} {'volume':<7} {'ped':>4} {'fora':>4} {'simples R$':>10} {'região R$':>10} {'B R$':>8} {'B x simples':>12} {'B x região':>11}  cam/dia simples|região|B")
    for s in semanas:
        a, r, b = s["bases"]["simples"], s["bases"]["regiao"], s["politicas"][0]
        print(f"{s['seed']:>4} {s['volume']:<7} {s['pedidos']:>4} {s['fora']:>4} {a['custo']:>10.0f} {r['custo']:>10.0f} {b['custo']:>8.0f} {eco(s, 'simples', 0):>11.1f}% {eco(s, 'regiao', 0):>10.1f}%"
              + f"  {max(a['caminhoes_dia'])}|{max(r['caminhoes_dia'])}|{max(b['caminhoes_dia'])}" + ("  FROTA LIVRE" if s["frota_livre"] else ""))
    for b in ("simples", "regiao"):
        print(f"\nContra o manual {'por região' if b == 'regiao' else 'simples'} (custo médio R$ {custo_base[b]['media']:.0f}/semana){'  <- linha de base forte' if b == forte else ''}:")
        for r in resumo[b]:
            e = r["economia_pct"]
            print(f"  {r['nome']:<40} economia média {e['media']:>5.1f}%  (mín {e['min']:.1f}%, máx {e['max']:.1f}%, desvio {e['desvio']:.1f})  "
                  + "  ".join(f"{v}: {x['media']:.1f}%" for v, x in r["por_volume"].items()))
    print(f"\nO manual por região saiu mais barato que o simples em {saida['regiao_ganha_em']} de {len(semanas)} semanas.")
    print(f"\nSemanas em que o plano não coube na frota e rodou com frota livre: {saida['frota_livre']} de {len(semanas)}")
    print("Detalhes em saida/semanas.json")


if __name__ == "__main__":
    main()
