"""Modelo EXATO (programação linear inteira) x heurística do solver Rust.

Para instâncias pequenas (subconjuntos dos pedidos), resolve o problema de roteamento de forma exata
com o HiGHS (scipy.optimize.milp) e compara com a heurística (Clarke-Wright + busca local + ILS).

Modelo (um dia, frota livre, sem custo fixo por caminhão, para isolar o roteamento):
  Conjuntos: nó 0 = depósito, 1..n = pedidos.
  Variáveis:
    x_ij ∈ {0,1}  o caminhão vai direto de i para j
    f_ij ≥ 0      carga (t) transportada no trecho i->j      (fluxo de mercadoria)
    T_j  ≥ 0      horas desde o início da viagem até chegar em j
  Objetivo (R$):
    min Σ_ij [ diesel·L_ij·(x_ij + α·f_ij/Q) + motorista·t_ij·x_ij ]
        + motorista·carregar·Σ_j x_0j + motorista·descarga·n
    (L_ij = litros com caminhão vazio; o consumo cresce com a carga f_ij -> continua LINEAR)
  Restrições:
    Σ_i x_ij = 1, Σ_k x_jk = 1                 cada pedido visitado uma vez
    Σ_i f_ij − Σ_k f_jk = d_j                    o caminhão deixa d_j toneladas em j
    d_j·x_ij ≤ f_ij ≤ (Q − d_i)·x_ij;  f_i0 = 0  capacidade (e elimina subciclos)
    T_j ≥ T_i + descarga + t_ij − M(1 − x_ij)    tempo acumulado (MTZ com tempo)
    T_j ≥ carregar + t_0j − M(1 − x_0j)        (0 ≤ T_j ≤ jornada)
    T_i + descarga + t_i0 ≤ jornada + M(1 − x_i0)

Uso: python3 scripts/exato.py [tamanhos...]   ex.: python3 scripts/exato.py 6 8 10 12
Requer: scipy (pip install scipy). Premissas lidas de dados/premissas.toml (o mesmo arquivo do solver).
"""
import csv, json, os, shutil, subprocess, sys, tempfile, time, tomllib
from pathlib import Path

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

RAIZ = Path(__file__).resolve().parent.parent
LIMITE_S = 300  # tempo máximo por instância no solver exato


def premissas():
    """dados/premissas.toml, com as seções achatadas num dicionário só: {"diesel_rs_l": 7.31, ...}."""
    with open(RAIZ / "dados/premissas.toml", "rb") as f:
        secoes = tomllib.load(f)
    return {k: float(v) for sec in secoes.values() for k, v in sec.items()}


def ler_matriz(nome):
    return np.array([[float(x) for x in l.split(",")] for l in (RAIZ / "dados" / nome).read_text().splitlines() if l.strip()])


def preparar(P):
    km, h = ler_matriz("matriz_km.csv"), ler_matriz("matriz_h.csv")
    with np.errstate(divide="ignore", invalid="ignore"):
        v = np.where(h > 0, km / h, 60.0)
    consumo = P["consumo_base_l_km"] * (1 + P["consumo_lento_extra"] * np.clip((P["vel_ref_kmh"] - v) / P["vel_ref_kmh"], 0, None))
    L = km * consumo
    t = h * P["fator_caminhao"]
    if (RAIZ / "dados/matriz_l.csv").exists():
        # matrizes de caminhão: tempo e litros já vêm prontos (este modelo usa só o caminho mais rápido de cada par)
        L, t = ler_matriz("matriz_l.csv"), h.copy()
    np.fill_diagonal(L, 0); np.fill_diagonal(t, 0)
    ped = list(csv.DictReader(open(RAIZ / "dados/pedidos.csv", encoding="utf-8")))
    return km, h, L, t, ped, (ler_matriz("matriz_l.csv") if (RAIZ / "dados/matriz_l.csv").exists() else None)


def resolver_exato(idx, dem, L, t, P, relaxado=False):
    """idx: índices dos pontos na matriz global (0 = depósito primeiro)."""
    n = len(idx) - 1
    N = n + 1
    Q, a = P["capacidade_t"], P["alpha_carga"]
    s, c0, J = P["descarga_h"], P["carregar_h"], P["jornada_h"]
    preco, rate = P["diesel_rs_l"], P["motorista_rs_h"]
    Ls = L[np.ix_(idx, idx)]
    ts = t[np.ix_(idx, idx)]
    arcos = [(i, j) for i in range(N) for j in range(N) if i != j]
    A = len(arcos)
    ix = {arc: k for k, arc in enumerate(arcos)}       # x
    jf = {arc: A + k for k, arc in enumerate(arcos)}   # f
    jT = lambda j: 2 * A + (j - 1)                     # T_j, j=1..n
    nv = 2 * A + n
    d = [0.0] + list(dem)

    c = np.zeros(nv)
    for (i, j) in arcos:
        c[ix[i, j]] = preco * Ls[i, j] + rate * ts[i, j] + (rate * c0 if i == 0 else 0)
        c[jf[i, j]] = preco * Ls[i, j] * a / Q
    const = rate * s * n

    lo, hi = np.zeros(nv), np.full(nv, np.inf)
    for (i, j) in arcos:
        hi[ix[i, j]] = 1
        hi[jf[i, j]] = 0 if j == 0 else Q - d[i]
    # Sem limites "apertados" do tipo T_j >= carregar + t_0j: a matriz real NÃO garante a
    # desigualdade triangular (às vezes passar por outra fazenda é mais rápido que o trecho direto),
    # e esses limites cortariam soluções válidas.
    for j in range(1, N):
        hi[jT(j)] = J
    M = J + s + ts.max()

    linhas, lb, ub = [], [], []
    def add(coefs, l, u):
        linhas.append(coefs); lb.append(l); ub.append(u)
    for j in range(1, N):
        add({ix[i, j]: 1 for i in range(N) if i != j}, 1, 1)
        add({ix[j, k]: 1 for k in range(N) if k != j}, 1, 1)
        fl = {jf[i, j]: 1 for i in range(N) if i != j}
        for k in range(N):
            if k != j: fl[jf[j, k]] = -1
        add(fl, d[j], d[j])
    for (i, j) in arcos:
        if j != 0:
            add({jf[i, j]: 1, ix[i, j]: -d[j]}, 0, np.inf)               # f >= d_j x
        add({jf[i, j]: 1, ix[i, j]: -(Q - d[i])}, -np.inf, 0)            # f <= (Q-d_i) x
        if i != 0 and j != 0:
            add({jT(j): 1, jT(i): -1, ix[i, j]: -M}, s + ts[i, j] - M, np.inf)
        elif i == 0:
            add({jT(j): 1, ix[0, j]: -M}, c0 + ts[0, j] - M, np.inf)
        else:
            add({jT(i): 1, ix[i, 0]: M}, -np.inf, J - s - ts[i, 0] + M)
    Am = lil_matrix((len(linhas), nv))
    for r, coefs in enumerate(linhas):
        for k, v in coefs.items():
            Am[r, k] = v
    integ = np.zeros(nv)
    if not relaxado:
        integ[:A] = 1
    t0 = time.time()
    r = milp(c, constraints=LinearConstraint(Am.tocsr(), lb, ub), integrality=integ, bounds=Bounds(lo, hi),
             options={"time_limit": LIMITE_S, "disp": False})
    dt = time.time() - t0
    if r.x is None:
        return None
    viagens = []
    if not relaxado:
        x = r.x
        for j in range(1, N):
            if x[ix[0, j]] > 0.5:
                v, cur = [], j
                while cur != 0:
                    v.append(cur)
                    cur = next(k for k in range(N) if k != cur and x[ix[cur, k]] > 0.5)
                viagens.append(v)
    gap = getattr(r, "mip_gap", 0.0) or 0.0
    return {"custo": r.fun + const, "tempo": dt, "otimo": r.status == 0, "gap": gap,
            "viagens": viagens, "vars": nv, "restricoes": len(linhas), "binarias": A,
            "limite_inf": (r.fun + const) if relaxado else None}


def heuristica(sel, ped, km, h, lit=None):
    """Roda o solver Rust no subconjunto (um dia só, frota livre, sem custo fixo)."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "dados").mkdir()
    with open(tmp / "dados/pedidos.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "nome", "lat", "lon", "demanda_t", "dia", "u"])
        for k, i in enumerate(sel, 1):
            p = ped[i - 1]
            w.writerow([k, p["nome"], p["lat"], p["lon"], p["demanda_t"], 0, 0.99])
    shutil.copy(RAIZ / "dados/premissas.toml", tmp / "dados/premissas.toml")
    idx = [0] + sel
    for nome, M in (("matriz_km.csv", km), ("matriz_h.csv", h)) + ((("matriz_l.csv", lit),) if lit is not None else ()):
        with open(tmp / "dados" / nome, "w") as f:
            for i in idx:
                f.write(",".join(f"{M[i, j]:.4f}" for j in idx) + "\n")
    exe = RAIZ / "solver/target/release/solver"
    if not exe.exists():
        subprocess.run(["cargo", "build", "--release", "-q"], cwd=RAIZ / "solver", check=True)
    env = dict(os.environ, CAMINHAO_RS_DIA="0", FROTA="999")
    t0 = time.time()
    subprocess.run([str(exe), str(tmp)], env=env, check=True, capture_output=True)
    dt = time.time() - t0
    res = json.load(open(tmp / "saida/resultado.json"))
    b = res["politicas"][1]
    shutil.rmtree(tmp)
    return {"custo": b["custo"], "tempo": dt, "viagens": [v["pedidos"] for v in b["dias"][0]]}


def main():
    tamanhos = [int(x) for x in sys.argv[1:]] or [6, 8, 10, 12, 14, 16]
    P = premissas()
    km, h, L, t, ped, lit = preparar(P)
    # pedidos que cabem numa jornada sozinhos (mesmo critério do solver)
    ok = [i for i in range(1, len(ped) + 1)
          if float(ped[i - 1]["demanda_t"]) <= P["capacidade_t"]
          and P["carregar_h"] + t[0, i] + P["descarga_h"] + t[i, 0] <= P["jornada_h"]]
    print(f"Diesel R$ {P['diesel_rs_l']:.2f}/L · caminhão {P['capacidade_t']:.0f} t · jornada {P['jornada_h']:.0f} h · limite {LIMITE_S}s por instância\n")
    cab = f"{'pedidos':>7} {'binárias':>8} {'restr.':>7} │ {'ótimo exato':>12} {'tempo':>8} │ {'heurística':>11} {'tempo':>7} {'gap':>7} │ {'relax. LP':>10}"
    print(cab); print("─" * len(cab))
    linhas = []
    for n in tamanhos:
        sel = ok[:n]
        dem = [float(ped[i - 1]["demanda_t"]) for i in sel]
        idx = [0] + sel
        ex = resolver_exato(idx, dem, L, t, P)
        lp = resolver_exato(idx, dem, L, t, P, relaxado=True)
        he = heuristica(sel, ped, km, h, lit)
        if ex is None:
            print(f"{n:>7}  sem solução viável em {LIMITE_S}s"); continue
        gap = max(0.0, (he["custo"] / ex["custo"] - 1) * 100) if he["custo"] >= ex["custo"] - 0.01 else (he["custo"] / ex["custo"] - 1) * 100
        marca = "" if ex["otimo"] else f" (limite! gap {ex['gap']*100:.1f}%)"
        print(f"{n:>7} {ex['binarias']:>8} {ex['restricoes']:>7} │ R$ {ex['custo']:>9,.2f} {ex['tempo']:>7.1f}s │ R$ {he['custo']:>8,.2f} {he['tempo']:>6.2f}s {gap:>6.2f}% │ R$ {lp['custo']:>7,.0f}{marca}"
              .replace(",", "X").replace(".", ",").replace("X", "."))
        linhas.append({"n": n, "exato": ex["custo"], "tempo_exato": ex["tempo"], "otimo_provado": ex["otimo"],
                       "heuristica": he["custo"], "tempo_heur": he["tempo"], "gap_pct": gap, "lp": lp["custo"],
                       "binarias": ex["binarias"], "restricoes": ex["restricoes"],
                       "viagens_exato": [[sel[k - 1] for k in v] for v in ex["viagens"]],
                       "viagens_heur": [[sel[k - 1] for k in v] for v in he["viagens"]]})
    (RAIZ / "saida").mkdir(exist_ok=True)
    # mescla por tamanho: mantém os n já calculados e substitui os que rodaram agora
    arq = RAIZ / "saida/exato.json"
    todas = {l["n"]: l for l in (json.load(open(arq, encoding="utf-8")) if arq.exists() else [])}
    todas.update({l["n"]: l for l in linhas})
    json.dump([todas[n] for n in sorted(todas)], open(arq, "w", encoding="utf-8"), indent=1)
    print("\ngap = quanto a heurística ficou acima do ótimo provado (0% = achou o ótimo)")
    print("relax. LP = limite inferior: o mesmo modelo deixando x_ij fracionário (o que o branch-and-bound usa)")
    print("Detalhes em saida/exato.json")


if __name__ == "__main__":
    main()
