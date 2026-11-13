"""Fase 3 do plano: tempo x combustível, separados (fronteira de Pareto).

O custo em R$ mistura diesel e hora de motorista. Aqui o solver roda a mesma instância várias
vezes mudando só o PESO de cada um na busca, de "só diesel" (lambda = 0) até "só tempo"
(lambda = 1); lambda = 0,5 é o custo real em R$. Para cada plano do mesmo dia (política B)
guardamos horas, litros e custo real, e descartamos os dominados (pior nos dois ao mesmo tempo).

  PESO_DIESEL = 2 * (1 - lambda)      PESO_TEMPO = 2 * lambda

Três planos ganham nome: mais rápido (menos horas), mais econômico (menos litros) e
equilibrado (menor custo real em R$).

Também roda os três extremos (lambda 0, 0,5 e 1) nas semanas de saida/semanas.json, para ver
se o tamanho da troca se repete.

Saída: saida/pareto.json
Uso: python3 scripts/pareto.py
"""
import json, os, shutil, statistics, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import semanas as sem

RAIZ = Path(__file__).resolve().parent.parent
LAMBDAS = [round(k * 0.05, 2) for k in range(21)]
SOLVER = RAIZ / "solver/target/release/solver"


def pesos(lam):
    return {"PESO_DIESEL": f"{2 * (1 - lam):.4f}", "PESO_TEMPO": f"{2 * lam:.4f}", "SO_MESMO_DIA": "1"}


def referencia(lam):
    """Roda o solver na instância de referência com o peso lam e devolve a política B."""
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "dados").mkdir()
        for arq in (RAIZ / "dados/referencia").glob("*.csv"):
            shutil.copy(arq, tmp / "dados" / arq.name)
        shutil.copy(RAIZ / "dados/premissas.toml", tmp / "dados/premissas.toml")
        env = {k: v for k, v in os.environ.items() if k not in ("FROTA", "CAMINHAO_RS_DIA")}
        env.update(pesos(lam))
        subprocess.run([str(SOLVER), str(tmp)], env=env, check=True, capture_output=True)
        b = json.load(open(tmp / "saida/resultado.json", encoding="utf-8"))["politicas"][1]
        b["lam"] = lam
        return b
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def marca_fronteira(pts):
    for p in pts:
        p["fronteira"] = not any((q["horas"] <= p["horas"] and q["litros"] <= p["litros"])
                                 and (q["horas"] < p["horas"] or q["litros"] < p["litros"]) for q in pts)


def nomeia(pts):
    return {"rapido": min(pts, key=lambda p: (p["horas"], p["litros"])),
            "economico": min(pts, key=lambda p: (p["litros"], p["horas"])),
            "equilibrado": min(pts, key=lambda p: (p["custo"], p["horas"]))}


def troca(pl):
    """Tamanho da troca entre o plano mais rápido e o mais econômico."""
    r, e = pl["rapido"], pl["economico"]
    dh, dl = e["horas"] - r["horas"], r["litros"] - e["litros"]
    return {"horas": round(dh, 1), "litros": round(dl, 1), "rs": round(r["custo"] - e["custo"], 2),
            "horas_por_100l": round(dh / dl * 100, 2) if dl > 0 else None,
            "litros_por_hora": round(dl / dh, 1) if dh > 0 else None}


def semana(seed, fazendas, km, h, frota_livre):
    _, pedidos = sem.sortear(seed, fazendas)
    pts = []
    for lam in (0.0, 0.5, 1.0):
        res, erro = sem.rodar_solver(pedidos, km, h, frota_livre, pesos(lam))
        if res is None and "frota estourada" in erro:
            res, erro = sem.rodar_solver(pedidos, km, h, True, pesos(lam))
        if res is None:
            raise RuntimeError(f"semana {seed}, lambda {lam}:\n{erro}")
        b = res["politicas"][1]
        pts.append({"lam": lam, "horas": b["horas"], "litros": b["litros"], "custo": b["custo"]})
    return {"seed": seed, "pontos": pts, "troca": troca(nomeia(pts))}


def main():
    subprocess.run(["cargo", "build", "--release", "-q"], cwd=RAIZ / "solver", check=True)
    nt = max(1, (os.cpu_count() or 2) - 1)
    with ThreadPoolExecutor(max_workers=nt) as ex:
        brutos = list(ex.map(referencia, LAMBDAS))
    # planos iguais aparecem em vários pesos: fica um de cada (horas, litros, custo)
    unicos = {}
    for b in brutos:
        unicos.setdefault((b["horas"], b["litros"], b["custo"]), b)
    pts = sorted(unicos.values(), key=lambda p: (p["horas"], p["litros"]))
    marca_fronteira(pts)
    planos = nomeia(pts)
    enx = lambda p: {k: p[k] for k in ("lam", "horas", "litros", "custo", "km", "viagens", "caminhoes_dia", "fronteira")}
    saida = {"lambdas": LAMBDAS, "pontos": [enx(p) for p in pts],
             "planos": {k: {**enx(p), "dias": p["dias"]} for k, p in planos.items()}, "troca": troca(planos)}

    arq_sem = RAIZ / "saida/semanas.json"
    if arq_sem.exists():
        sm = json.load(open(arq_sem, encoding="utf-8"))
        fazendas = list(sem.csv.DictReader(open(sem.BASE / "fazendas.csv", encoding="utf-8")))
        km, h = sem.ler_matriz(sem.BASE / "matriz_km.csv"), sem.ler_matriz(sem.BASE / "matriz_h.csv")
        with ThreadPoolExecutor(max_workers=nt) as ex:
            ws = list(ex.map(lambda w: semana(w["seed"], fazendas, km, h, w["frota_livre"]), sm["semanas"]))
        est = lambda xs: {"media": round(statistics.mean(xs), 1), "min": round(min(xs), 1), "max": round(max(xs), 1)}
        saida["semanas"] = {"n": len(ws), "horas": est([w["troca"]["horas"] for w in ws]),
                            "litros": est([w["troca"]["litros"] for w in ws]), "rs": est([w["troca"]["rs"] for w in ws]),
                            "detalhe": ws}

    json.dump(saida, open(RAIZ / "saida/pareto.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"Instância de referência, política B (tudo no mesmo dia): {len(brutos)} pesos -> {len(pts)} planos diferentes, {sum(p['fronteira'] for p in pts)} na fronteira\n")
    print(f"{'lambda':>6} {'horas':>7} {'litros':>8} {'R$':>10} {'km':>7} {'viag.':>5}  fronteira")
    for p in pts:
        nome = " ".join(k for k, q in planos.items() if q is p)
        print(f"{p['lam']:>6.2f} {p['horas']:>7.1f} {p['litros']:>8.1f} {p['custo']:>10.2f} {p['km']:>7.1f} {p['viagens']:>5}  {'sim' if p['fronteira'] else 'não'}  {nome}")
    t = saida["troca"]
    print(f"\nMais rápido x mais econômico: {t['horas']} h a menos custam {t['litros']} L a mais (R$ {t['rs']:.2f} a mais no total).")
    if "semanas" in saida:
        s = saida["semanas"]
        print(f"Nas {s['n']} semanas simuladas (3 pesos cada): o plano mais rápido poupa em média {s['horas']['media']} h "
              f"(de {s['horas']['min']} a {s['horas']['max']}) e gasta {s['litros']['media']} L a mais (de {s['litros']['min']} a {s['litros']['max']}); "
              f"custa em média R$ {s['rs']['media']} a mais (de {s['rs']['min']} a {s['rs']['max']}).")
    print("Detalhes em saida/pareto.json")


if __name__ == "__main__":
    main()
