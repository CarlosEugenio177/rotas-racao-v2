"""Fase 4 do plano: ótimo provado no problema COMPLETO (com frota e custo por caminhão).

O scripts/exato.py prova o ótimo do roteamento de um dia com frota livre e sem custo fixo.
Este script resolve o problema que o solver Rust resolve de verdade:
  - cada caminhão usado no dia custa caminhao_rs_dia;
  - no máximo `frota` caminhões por dia;
  - um caminhão pode fazer várias viagens no dia, desde que a soma caiba na jornada;
  - (opcional) cada pedido pode ser entregue em qualquer dia da sua janela.

MÉTODO PRINCIPAL: enumeração completa + partição exata (resolver_enum).
  a) Para cada conjunto de pedidos que cabe num caminhão, programação dinâmica acha TODAS as
     ordens de visita não dominadas em (custo, horas). O consumo depende da carga, e a carga em
     cada trecho só depende de quem ainda falta entregar, então a recorrência é exata.
  b) Um "dia de caminhão" é um conjunto de viagens do mesmo caminhão cuja soma de horas cabe na
     jornada. Para cada conjunto de pedidos U, outra programação dinâmica acha o dia de caminhão
     mais barato que atende exatamente U (ou conclui que não existe).
  c) Um modelo de partição (HiGHS) escolhe dias de caminhão que cobrem cada pedido exatamente
     uma vez, com no máximo `frota` por dia, ao menor custo (cada um paga caminhao_rs_dia).
  Como (a) e (b) enumeram tudo, o resultado de (c) é o ótimo do problema completo.

MÉTODO DE CONFERÊNCIA: modelo compacto com big-M (resolver_milp), formulação independente.
Ele é fraco (não prova o ótimo de um dia inteiro em minutos), então só é usado numa instância
bem pequena para conferir que os dois métodos dão o mesmo custo. Para cada dia d:
  xa[d,i,j]  o caminhão vai direto do pedido i para o pedido j (mesma viagem)
  xs[d,j]    um caminhão COMEÇA o dia indo do depósito para j        (conta 1 caminhão)
  xe[d,i]    um caminhão TERMINA o dia voltando de i para o depósito
  y[d,i,j]   depois de i o caminhão volta ao depósito, recarrega e sai para j (nova viagem, mesmo caminhão)
  fa, fs, g  toneladas no caminhão em cada um desses trechos (fluxo de carga)
  z[j,d]     o pedido j é entregue no dia d
  T[j]       horas desde o início do dia do caminhão até chegar em j (acumula entre viagens)
Cada pedido tem uma entrada e uma saída no dia em que é entregue; a carga cai d_j em cada parada;
T cresce ao longo do dia do caminhão e o retorno final tem que caber na jornada. O número de
caminhões do dia é a soma de xs. Como T acumula entre viagens, empacotar viagens em caminhões
faz parte do modelo (o solver Rust usa a heurística FFD para isso).

Compara com o solver Rust:
  1) política B (tudo no mesmo dia): os 5 dias da instância de referência, um modelo por dia;
  2) política C com 100% flexível: uma semana pequena (3 dias, ~5 pedidos por dia), com o dia de
     entrega como variável.

Com --semanas, repete as duas comparações em todas as semanas de saida/semanas.json
(100 dias no mesmo dia + 20 semanas pequenas flexíveis) e grava saida/exato_semanas.json.

Saída: saida/exato_frota.json
Uso: python3 scripts/exato_frota.py [limite_s=300]
     python3 scripts/exato_frota.py --semanas
"""
import csv, json, os, shutil, subprocess, sys, tempfile, time, tomllib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

RAIZ = Path(__file__).resolve().parent.parent
REF = RAIZ / "dados/referencia"
MODO_SEMANAS = "--semanas" in sys.argv
_args = [a for a in sys.argv[1:] if not a.startswith("--")]
LIMITE_S = int(_args[0]) if _args else 300


def premissas():
    with open(RAIZ / "dados/premissas.toml", "rb") as f:
        return {k: float(v) for sec in tomllib.load(f).values() for k, v in sec.items()}


MATRIZES = ("matriz_km.csv", "matriz_h.csv", "matriz_l.csv", "matriz_km_eco.csv", "matriz_h_eco.csv", "matriz_l_eco.csv")


def carregar(P, pasta=REF):
    ler = lambda nome: np.array([[float(x) for x in l.split(",")] for l in (pasta / nome).read_text().splitlines() if l.strip()])
    ped = list(csv.DictReader(open(pasta / "pedidos.csv", encoding="utf-8")))
    return montar(P, {nome: ler(nome) for nome in MATRIZES if (pasta / nome).exists()}, ped)


def montar(P, mats, ped):
    """mats: matrizes por nome de arquivo, com o depósito no índice 0 e o pedido i no índice i; ped: linhas de pedidos.csv.
    Com matriz_l.csv as matrizes são de caminhão (tempo e litros prontos); senão, perfil de carro (tempo x fator, consumo estimado).
    Com as matrizes _eco há um segundo caminho por par, e cada trecho usa o de menor custo para a carga do momento."""
    km, h = mats["matriz_km.csv"], mats["matriz_h.csv"]
    if "matriz_l.csv" in mats:
        L, t = mats["matriz_l.csv"].copy(), h.copy()
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            v = np.where(h > 0, km / h, 60.0)
        L = km * P["consumo_base_l_km"] * (1 + P["consumo_lento_extra"] * np.clip((P["vel_ref_kmh"] - v) / P["vel_ref_kmh"], 0, None))
        t = h * P["fator_caminhao"]
    np.fill_diagonal(L, 0); np.fill_diagonal(t, 0)
    eco = None
    if "matriz_l.csv" in mats and "matriz_l_eco.csv" in mats:
        eco = {"L": mats["matriz_l_eco.csv"].copy(), "t": mats["matriz_h_eco.csv"].copy()}
        np.fill_diagonal(eco["L"], 0); np.fill_diagonal(eco["t"], 0)
    dem = [0.0] + [float(p["demanda_t"]) for p in ped]
    dia = [0] + [int(p["dia"]) for p in ped]
    I = {"mats": mats, "km": km, "h": h, "L": L, "t": t, "eco": eco, "ped": ped, "dem": dem, "dia": dia}
    I["ativo"] = [False] + [dem[i] <= P["capacidade_t"] and P["carregar_h"] + arco(I, P, 0, i, dem[i])[0] + P["descarga_h"] + arco(I, P, i, 0, 0.0)[0] <= P["jornada_h"]
                            for i in range(1, len(dem))]
    return I


def arco(I, P, i, j, carga):
    """Trecho i -> j com `carga` toneladas: (horas, litros). Com dois caminhos, o de menor custo (mesma regra do solver)."""
    fator = 1 + P["alpha_carga"] * carga / P["capacidade_t"]
    h1, l1 = I["t"][i, j], I["L"][i, j] * fator
    if I["eco"] is not None:
        h2, l2 = I["eco"]["t"][i, j], I["eco"]["L"][i, j] * fator
        if P["diesel_rs_l"] * l2 + P["motorista_rs_h"] * h2 < P["diesel_rs_l"] * l1 + P["motorista_rs_h"] * h1 - 1e-9:
            return h2, l2
    return h1, l1


# ---------- avaliação em Python, espelho do avalia_viagem do solver (para conferir os dois lados) ----------
def avalia_viagem(I, P, v):
    carga = sum(I["dem"][i] for i in v)
    h, litros, prev = P["carregar_h"], 0.0, 0
    for i in list(v) + [0]:
        dh, dl = arco(I, P, prev, i, carga)
        h += dh
        litros += dl
        if i:
            h += P["descarga_h"]; carga -= I["dem"][i]
        prev = i
    return {"h": h, "litros": litros, "custo": litros * P["diesel_rs_l"] + h * P["motorista_rs_h"]}


def custo_dia(I, P, caminhoes):
    """caminhoes = lista de caminhões; cada caminhão = lista de viagens. Confere jornada e capacidade."""
    c = 0.0
    for cam in caminhoes:
        horas = 0.0
        for v in cam:
            a = avalia_viagem(I, P, v)
            assert sum(I["dem"][i] for i in v) <= P["capacidade_t"] + 1e-9, "carga estourada"
            c += a["custo"]; horas += a["h"]
        assert horas <= P["jornada_h"] + 1e-6, f"jornada estourada: {horas:.3f} h"
        c += P["caminhao_rs_dia"]
    return c


# ---------- método principal: enumeração completa + partição ----------
def _pareto(labels):
    """Mantém só os rótulos não dominados em (custo, horas). labels: listas [custo, horas, ...]."""
    labels.sort(key=lambda l: (l[0], l[1]))
    out, melhor_h = [], float("inf")
    for l in labels:
        if l[1] < melhor_h - 1e-12:
            out.append(l); melhor_h = l[1]
    return out


def enumerar(I, P, clientes):
    """Devolve W: máscara de pedidos -> (custo mínimo do dia de caminhão, lista de viagens)."""
    Q, alfa = P["capacidade_t"], P["alpha_carga"]
    s, c0, J = P["descarga_h"], P["carregar_h"], P["jornada_h"]
    preco, rate = P["diesel_rs_l"], P["motorista_rs_h"]
    L, t, dem = I["L"], I["t"], I["dem"]
    n = len(clientes)
    carga = [0.0] * (1 << n)
    for m in range(1, 1 << n):
        b = (m & -m).bit_length() - 1
        carga[m] = carga[m & (m - 1)] + dem[clientes[b]]
    # (a) G[m][u]: do momento em que chega em u (faltando entregar m, que contém u) até voltar ao depósito
    G, viagens = {}, {}
    for m in range(1, 1 << n):
        if carga[m] > Q + 1e-9:
            continue
        bits = [b for b in range(n) if m >> b & 1]
        Gm = {}
        for u in bits:
            cu, rem = clientes[u], m ^ (1 << u)
            if rem == 0:
                ah, al = arco(I, P, cu, 0, 0.0)
                labs = [[preco * al + rate * (s + ah), s + ah, None, None]]
            else:
                labs = []
                for v, lv in G[rem].items():
                    cv = clientes[v]
                    ah, al = arco(I, P, cu, cv, carga[rem])
                    dc, dh = preco * al + rate * (s + ah), s + ah
                    labs += [[dc + l[0], dh + l[1], v, k] for k, l in enumerate(lv) if dh + l[1] <= J]
                labs = _pareto(labs)
            if labs:
                Gm[u] = labs
        G[m] = Gm
        labs = []
        for u, lu in Gm.items():
            cu = clientes[u]
            ah, al = arco(I, P, 0, cu, carga[m])
            dc, dh = preco * al + rate * (c0 + ah), c0 + ah
            labs += [[dc + l[0], dh + l[1], u, k] for k, l in enumerate(lu) if dh + l[1] <= J + 1e-9]
        if labs:
            viagens[m] = _pareto(labs)

    def ordem(m, lab):
        seq, u, k = [], lab[2], lab[3]
        while u is not None:
            seq.append(clientes[u]); l = G[m][u][k]; m ^= 1 << u; u, k = l[2], l[3]
        return seq

    # (b) D[U]: dias de caminhão não dominados em (custo, horas) que atendem exatamente U
    D = {0: [[0.0, 0.0, None, None, None]]}
    for U in range(1, 1 << n):
        a = U & -U
        resto, sub, labs = U ^ a, U ^ a, []
        while True:
            S = sub | a
            vs, dr = viagens.get(S), D.get(U ^ S)
            if vs and dr:
                labs += [[v[0] + r[0], v[1] + r[1], S, iv, ir] for iv, v in enumerate(vs) for ir, r in enumerate(dr) if v[1] + r[1] <= J + 1e-9]
            if sub == 0:
                break
            sub = (sub - 1) & resto
        if labs:
            D[U] = _pareto(labs)

    def plano(U):
        lab, out = D[U][0], []
        while U:
            S, iv, ir = lab[2], lab[3], lab[4]
            out.append(ordem(S, viagens[S][iv])); U ^= S; lab = D[U][ir]
        return out

    return {U: D[U][0][0] for U in D if U}, plano, len(viagens)


def resolver_enum(I, P, clientes_dia):
    """Ótimo do problema completo. clientes_dia: por dia, os pedidos que PODEM ser entregues naquele dia."""
    t0 = time.time()
    todos = sorted({j for C in clientes_dia for j in C})
    pos = {j: k for k, j in enumerate(todos)}
    W, plano, nviag = enumerar(I, P, todos)
    cam, frota = P["caminhao_rs_dia"], int(P["frota"])
    cols, li, co = [], [], []
    for d, C in enumerate(clientes_dia):
        perm = sum(1 << pos[j] for j in C)
        for U, w in W.items():
            if U & ~perm:
                continue
            k = len(cols); cols.append((d, U, w + cam))
            for b in range(len(todos)):
                if U >> b & 1:
                    li.append(b); co.append(k)
            li.append(len(todos) + d); co.append(k)
    n, nd = len(todos), len(clientes_dia)
    A = coo_matrix((np.ones(len(li)), (li, co)), shape=(n + nd, len(cols))).tocsr()
    lb = np.r_[np.ones(n), np.zeros(nd)]; ub = np.r_[np.ones(n), np.full(nd, frota)]
    r = milp(np.array([c[2] for c in cols]), constraints=LinearConstraint(A, lb, ub),
             integrality=np.ones(len(cols)), bounds=Bounds(0, 1), options={"disp": False, "mip_rel_gap": 0.0})
    out = {"tempo": time.time() - t0, "viagens_possiveis": nviag, "dias_de_caminhao": len(W), "colunas": len(cols),
           "status": int(r.status), "otimo_provado": r.status == 0, "custo": None, "limite_inferior": None, "dias": None,
           "binarias": len(cols), "restricoes": n + nd}
    if r.x is None:
        return out
    dias = [[] for _ in clientes_dia]
    for k, (d, U, _) in enumerate(cols):
        if r.x[k] > 0.5:
            dias[d].append(plano(U))
    out["custo"] = float(r.fun)
    out["limite_inferior"] = float(r.mip_dual_bound) if r.status == 0 else None
    out["dias"] = dias
    conferido = sum(custo_dia(I, P, cams) for cams in dias)
    assert abs(conferido - out["custo"]) < 0.05, f"partição {out['custo']:.2f} x avaliação {conferido:.2f}"
    assert sorted(j for cams in dias for c_ in cams for v in c_ for j in v) == todos, "pedido faltando ou repetido"
    return out


# ---------- método de conferência: modelo compacto com big-M ----------
def resolver_milp(I, P, clientes_dia, limite_s):
    """clientes_dia: lista (um item por dia) com os pedidos que PODEM ser entregues naquele dia.
    Um pedido que aparece em mais de um dia é entregue em exatamente um deles."""
    Q, alfa = P["capacidade_t"], P["alpha_carga"]
    s, c0, J = P["descarga_h"], P["carregar_h"], P["jornada_h"]
    preco, rate, cam, frota = P["diesel_rs_l"], P["motorista_rs_h"], P["caminhao_rs_dia"], int(P["frota"])
    L, t, dem = I["L"], I["t"], I["dem"]
    todos = sorted({j for C in clientes_dia for j in C})
    col, custo, lo, hi, inteira = {}, [], [], [], []

    def var(chave, c, l, u, bin_):
        col[chave] = len(custo); custo.append(c); lo.append(l); hi.append(u); inteira.append(1 if bin_ else 0)

    for d, C in enumerate(clientes_dia):
        for j in C:
            var(("xs", d, j), preco * L[0, j] + rate * (c0 + t[0, j]) + cam, 0, 1, True)
            var(("fs", d, j), preco * L[0, j] * alfa / Q, 0, Q, False)
            var(("xe", d, j), preco * L[j, 0] + rate * t[j, 0], 0, 1, True)
            for k in C:
                if k == j: continue
                var(("xa", d, j, k), preco * L[j, k] + rate * t[j, k], 0, 1, True)
                var(("fa", d, j, k), preco * L[j, k] * alfa / Q, 0, Q - dem[j], False)
                var(("y", d, j, k), preco * (L[j, 0] + L[0, k]) + rate * (t[j, 0] + c0 + t[0, k]), 0, 1, True)
                var(("g", d, j, k), preco * L[0, k] * alfa / Q, 0, Q, False)
    dias_de = {j: [d for d, C in enumerate(clientes_dia) if j in C] for j in todos}
    for j in todos:
        for d in dias_de[j]:
            unico = len(dias_de[j]) == 1
            var(("z", j, d), 0, 1 if unico else 0, 1, True)
        var(("T", j), 0, 0, J, False)
    const = rate * s * len(todos)
    M = J + s + c0 + 2 * max(t[i, k] for i in [0] + todos for k in [0] + todos)

    li, co, va, lb, ub = [], [], [], [], []
    def add(coefs, l, u):
        r = len(lb)
        for k, v in coefs.items():
            li.append(r); co.append(col[k]); va.append(v)
        lb.append(l); ub.append(u)

    for j in todos:
        add({("z", j, d): 1 for d in dias_de[j]}, 1, 1)
    for d, C in enumerate(clientes_dia):
        for j in C:
            outros = [i for i in C if i != j]
            ent = {("xs", d, j): 1, ("z", j, d): -1}
            sai = {("xe", d, j): 1, ("z", j, d): -1}
            flu = {("fs", d, j): 1, ("z", j, d): -dem[j]}
            for i in outros:
                ent[("xa", d, i, j)] = 1; ent[("y", d, i, j)] = 1
                sai[("xa", d, j, i)] = 1; sai[("y", d, j, i)] = 1
                flu[("fa", d, i, j)] = 1; flu[("g", d, i, j)] = 1; flu[("fa", d, j, i)] = -1
            add(ent, 0, 0); add(sai, 0, 0); add(flu, 0, 0)
            add({("fs", d, j): 1, ("xs", d, j): -dem[j]}, 0, np.inf)
            add({("fs", d, j): 1, ("xs", d, j): -Q}, -np.inf, 0)
            add({("T", j): 1, ("xs", d, j): -M}, c0 + t[0, j] - M, np.inf)
            add({("T", j): 1, ("xe", d, j): M}, -np.inf, J - s - t[j, 0] + M)
            for k in outros:
                add({("fa", d, j, k): 1, ("xa", d, j, k): -dem[k]}, 0, np.inf)
                add({("fa", d, j, k): 1, ("xa", d, j, k): -(Q - dem[j])}, -np.inf, 0)
                add({("g", d, j, k): 1, ("y", d, j, k): -dem[k]}, 0, np.inf)
                add({("g", d, j, k): 1, ("y", d, j, k): -Q}, -np.inf, 0)
                add({("T", k): 1, ("T", j): -1, ("xa", d, j, k): -M}, s + t[j, k] - M, np.inf)
                add({("T", k): 1, ("T", j): -1, ("y", d, j, k): -M}, s + t[j, 0] + c0 + t[0, k] - M, np.inf)
        add({("xs", d, j): 1 for j in C}, 0, frota)

    A = coo_matrix((va, (li, co)), shape=(len(lb), len(custo))).tocsr()
    t0 = time.time()
    r = milp(np.array(custo), constraints=LinearConstraint(A, lb, ub), integrality=np.array(inteira),
             bounds=Bounds(lo, hi), options={"time_limit": limite_s, "disp": False})
    dt = time.time() - t0
    out = {"tempo": dt, "binarias": int(sum(inteira)), "restricoes": len(lb), "status": int(r.status),
           "otimo_provado": r.status == 0, "limite_inferior": None, "custo": None, "dias": None}
    if getattr(r, "mip_dual_bound", None) is not None and np.isfinite(r.mip_dual_bound):
        out["limite_inferior"] = float(r.mip_dual_bound) + const
    if r.x is None:
        return out
    x = r.x
    on = lambda *ch: x[col[ch]] > 0.5
    dias = []
    for d, C in enumerate(clientes_dia):
        caminhoes = []
        for j0 in C:
            if not on("xs", d, j0): continue
            viagens, v, cur = [], [j0], j0
            while True:
                if on("xe", d, cur):
                    viagens.append(v); break
                prox = next((k for k in C if k != cur and on("xa", d, cur, k)), None)
                if prox is not None:
                    v.append(prox); cur = prox; continue
                prox = next(k for k in C if k != cur and on("y", d, cur, k))
                viagens.append(v); v = [prox]; cur = prox
            caminhoes.append(viagens)
        dias.append(caminhoes)
    out["custo"] = float(r.fun) + const
    out["dias"] = dias
    # confere a função objetivo do modelo com a avaliação independente do plano
    conferido = sum(custo_dia(I, P, cams) for cams in dias)
    assert abs(conferido - out["custo"]) < 0.05, f"modelo {out['custo']:.2f} x avaliação {conferido:.2f}"
    entregues = sorted(j for cams in dias for cam_ in cams for v in cam_ for j in v)
    assert entregues == todos, "pedido faltando ou repetido na solução exata"
    return out


def _um_dia(args):
    d, C = args
    P = premissas(); I = carregar(P)
    return d, resolver_enum(I, P, [C])


# ---------- solver Rust num subconjunto ----------
def heuristica(I, sel, frota_livre=False):
    """Roda o solver Rust só com os pedidos de `sel` (custo fixo normal; frota normal ou livre). Devolve o resultado.json."""
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "dados").mkdir()
        shutil.copy(RAIZ / "dados/premissas.toml", tmp / "dados/premissas.toml")
        with open(tmp / "dados/pedidos.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "nome", "lat", "lon", "demanda_t", "dia", "u"])
            for k, i in enumerate(sel, 1):
                p = I["ped"][i - 1]
                w.writerow([k, p["nome"], p["lat"], p["lon"], p["demanda_t"], p["dia"], p["u"]])
        idx = [0] + sel
        for nome, M in I["mats"].items():
            with open(tmp / "dados" / nome, "w") as f:
                for i in idx:
                    f.write(",".join(f"{M[i, j]:.4f}" for j in idx) + "\n")
        env = {k: v for k, v in os.environ.items() if k not in ("FROTA", "CAMINHAO_RS_DIA", "PESO_DIESEL", "PESO_TEMPO")}
        if frota_livre:
            env["FROTA"] = "99"
        subprocess.run([str(RAIZ / "solver/target/release/solver"), str(tmp)], env=env, check=True, capture_output=True)
        res = json.load(open(tmp / "saida/resultado.json", encoding="utf-8"))
        for pol in res["politicas"]:      # volta para os índices originais
            pol["dias"] = [[[sel[k - 1] for k in v["pedidos"]] for v in dia] for dia in pol["dias"]]
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def custo_heur_dia(I, P, viagens, n_caminhoes):
    return sum(avalia_viagem(I, P, v)["custo"] for v in viagens) + n_caminhoes * P["caminhao_rs_dia"]


def linha(nome, n, ex, heur):
    gap = (heur / ex["custo"] - 1) * 100 if ex["custo"] else None
    li = ex["limite_inferior"]
    gap_lim = (heur / li - 1) * 100 if li else None
    return {"nome": nome, "pedidos": n, "heuristica": heur, "exato": ex["custo"], "otimo_provado": ex["otimo_provado"],
            "limite_inferior": li, "gap_pct": gap, "gap_max_pct": gap_lim, "tempo_exato": ex["tempo"],
            "binarias": ex["binarias"], "restricoes": ex["restricoes"], "plano_exato": ex["dias"],
            "viagens_possiveis": ex.get("viagens_possiveis"), "dias_de_caminhao": ex.get("dias_de_caminhao")}


def mostra(l):
    ex = f"R$ {l['exato']:>9.2f}" if l["exato"] is not None else "   (sem solução)"
    li = f"R$ {l['limite_inferior']:>9.2f}" if l["limite_inferior"] is not None else "-"
    gap = f"{l['gap_pct']:>6.2f}%" if l["gap_pct"] is not None else "   -"
    prov = "provado" if l["otimo_provado"] else f"NÃO provado (gap máx. {l['gap_max_pct']:.2f}%)" if l["gap_max_pct"] is not None else "NÃO provado"
    print(f"{l['nome']:<22} {l['pedidos']:>3} │ heurística R$ {l['heuristica']:>9.2f} │ exato {ex} │ gap {gap} │ limite inferior {li} │ {l['tempo_exato']:>6.1f}s │ {prov}")


# ---------- banco de testes: as semanas simuladas de saida/semanas.json ----------
def _uma_semana(args):
    seed, frota_livre = args
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import semanas as sem
    P = premissas()
    fazendas = list(csv.DictReader(open(sem.BASE / "fazendas.csv", encoding="utf-8")))
    MATS = {nome: np.array(M) for nome, M in sem.carregar_matrizes().items()}
    _, pedidos = sem.sortear(seed, fazendas)
    idx = [0] + [p["faz"] for p in pedidos]
    ped = [{"nome": p["nome"], "lat": p["lat"], "lon": p["lon"], "demanda_t": p["dem"], "dia": p["dia"], "u": p["u"]} for p in pedidos]
    I = montar(P, {nome: M[np.ix_(idx, idx)] for nome, M in MATS.items()}, ped)
    ativos = [i for i in range(1, len(I["dem"])) if I["ativo"][i]]
    ndias = max(I["dia"]) + 1
    por_dia = [[i for i in ativos if I["dia"][i] == d] for d in range(ndias)]
    Pex = dict(P, frota=99) if frota_livre else P
    b = heuristica(I, ativos, frota_livre)["politicas"][1]
    dias = []
    for d in range(ndias):
        ex = resolver_enum(I, Pex, [por_dia[d]])
        l = linha(f"semana {seed}, dia {d + 1}", len(por_dia[d]), ex, custo_heur_dia(I, P, b["dias"][d], b["caminhoes_dia"][d]))
        l.pop("plano_exato"); dias.append(l)
    # semana pequena com dia de entrega livre: 3 dias, 5 pedidos por dia, todos flexíveis
    DIAS_P, POR_DIA, folga = 3, 5, int(P["folga_dias"])
    sel = [i for d in range(DIAS_P) for i in por_dia[d][:POR_DIA]]
    jan = {i: (I["dia"][i], min(I["dia"][i] + folga, DIAS_P - 1)) for i in sel}
    c100 = heuristica(I, sel)["politicas"][-1]
    exf = resolver_enum(I, P, [[i for i in sel if jan[i][0] <= d <= jan[i][1]] for d in range(DIAS_P)])
    lf = linha(f"semana {seed}, 3 dias flex.", len(sel), exf, c100["custo"]); lf.pop("plano_exato")
    return {"seed": seed, "frota_livre": frota_livre, "dias": dias, "flex": lf}


def main_semanas():
    sm = json.load(open(RAIZ / "saida/semanas.json", encoding="utf-8"))
    subprocess.run(["cargo", "build", "--release", "-q"], cwd=RAIZ / "solver", check=True)
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1)) as ex:
        ws = list(ex.map(_uma_semana, [(w["seed"], w["frota_livre"]) for w in sm["semanas"]]))
    dias = [l for w in ws for l in w["dias"]]
    flex = [w["flex"] for w in ws]
    def resumo(ls):
        g = [l["gap_pct"] for l in ls]
        return {"n": len(ls), "provados": sum(l["otimo_provado"] for l in ls), "no_otimo": sum(x < 0.005 for x in g),
                "gap_medio_pct": round(float(np.mean(g)), 3), "gap_max_pct": round(max(g), 3), "gap_min_pct": round(min(g), 4),
                "pedidos_min": min(l["pedidos"] for l in ls), "pedidos_max": max(l["pedidos"] for l in ls),
                "custo_heur": round(sum(l["heuristica"] for l in ls), 2), "custo_exato": round(sum(l["exato"] for l in ls), 2)}
    out = {"mesmo_dia": resumo(dias), "flexivel": resumo(flex), "semanas": ws}
    json.dump(out, open(RAIZ / "saida/exato_semanas.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=lambda o: o.item())
    for nome, r, ls in (("Tudo no mesmo dia: cada dia das semanas simuladas", out["mesmo_dia"], dias),
                        ("Dia de entrega livre: 3 dias x 5 pedidos de cada semana", out["flexivel"], flex)):
        print(f"{nome}\n  {r['n']} instâncias de {r['pedidos_min']} a {r['pedidos_max']} pedidos, {r['provados']} com ótimo provado; "
              f"o programa achou o ótimo em {r['no_otimo']}; gap médio {r['gap_medio_pct']:.3f}%, máximo {r['gap_max_pct']:.3f}%, mínimo {r['gap_min_pct']:.4f}%")
        for l in sorted(ls, key=lambda l: -l["gap_pct"])[:6]:
            if l["gap_pct"] >= 0.005:
                print(f"    {l['nome']:<24} {l['pedidos']:>2} pedidos  heurística R$ {l['heuristica']:.2f}  ótimo R$ {l['exato']:.2f}  gap {l['gap_pct']:.2f}%")
    print(f"\n{time.time() - t0:.0f} s. Detalhes em saida/exato_semanas.json")
    if out["mesmo_dia"]["gap_min_pct"] < -1e-4 or out["flexivel"]["gap_min_pct"] < -1e-4:
        print("ATENÇÃO: gap NEGATIVO: o método exato está cortando soluções válidas. Isso é bug."); sys.exit(2)


def main():
    P = premissas(); I = carregar(P)
    subprocess.run(["cargo", "build", "--release", "-q"], cwd=RAIZ / "solver", check=True)
    ativos = [i for i in range(1, len(I["dem"])) if I["ativo"][i]]
    ndias = max(I["dia"]) + 1
    print(f"Diesel R$ {P['diesel_rs_l']:.2f}/L · frota {int(P['frota'])} · R$ {P['caminhao_rs_dia']:.0f} por caminhão/dia · jornada {P['jornada_h']:.0f} h · limite {LIMITE_S}s só no modelo de conferência\n")

    # 1) política B, dia a dia, na instância de referência
    res = heuristica(I, ativos)
    b = res["politicas"][1]
    por_dia = [[i for i in ativos if I["dia"][i] == d] for d in range(ndias)]
    with ProcessPoolExecutor(max_workers=min(ndias, max(1, (os.cpu_count() or 2) // 2))) as ex:
        exatos = dict(ex.map(_um_dia, [(d, por_dia[d]) for d in range(ndias)]))
    print("1) Tudo no mesmo dia (política B), semana de referência, com frota e custo por caminhão:")
    mesmo_dia = []
    for d in range(ndias):
        heur = custo_heur_dia(I, P, b["dias"][d], b["caminhoes_dia"][d])
        l = linha(f"dia {d + 1}", len(por_dia[d]), exatos[d], heur)
        l["caminhoes_heur"] = b["caminhoes_dia"][d]
        l["caminhoes_exato"] = len(exatos[d]["dias"][0]) if exatos[d]["dias"] else None
        mesmo_dia.append(l); mostra(l)
    tot_h = sum(l["heuristica"] for l in mesmo_dia)
    tot_e = sum(l["exato"] for l in mesmo_dia) if all(l["exato"] is not None for l in mesmo_dia) else None
    tot_li = sum(l["limite_inferior"] for l in mesmo_dia) if all(l["limite_inferior"] is not None for l in mesmo_dia) else None
    semana = {"heuristica": tot_h, "exato": tot_e, "limite_inferior": tot_li,
              "todos_provados": all(l["otimo_provado"] for l in mesmo_dia),
              "gap_pct": (tot_h / tot_e - 1) * 100 if tot_e else None,
              "gap_max_pct": (tot_h / tot_li - 1) * 100 if tot_li else None}
    print(f"{'semana':<22} {len(ativos):>3} │ heurística R$ {tot_h:>9.2f} │ exato R$ {tot_e or 0:>9.2f} │ gap {semana['gap_pct'] or 0:>6.2f}% │ limite inferior R$ {tot_li or 0:>9.2f} │ gap máx. {semana['gap_max_pct'] or 0:.2f}%")
    assert abs(tot_h - b["custo"]) < 0.5, f"custo da heurística recalculado ({tot_h:.2f}) difere do solver ({b['custo']:.2f})"

    # 2) semana pequena com dia de entrega livre (política C, 100% flexível)
    DIAS_P, POR_DIA = 3, 5
    sel = [i for d in range(DIAS_P) for i in por_dia[d][:POR_DIA]]
    folga = int(P["folga_dias"])
    janela = {i: (I["dia"][i], min(I["dia"][i] + folga, DIAS_P - 1)) for i in sel}
    res2 = heuristica(I, sel)
    c100 = res2["politicas"][-1]
    assert abs(c100["flex"] - 1.0) < 1e-9
    exf = resolver_enum(I, P, [[i for i in sel if janela[i][0] <= d <= janela[i][1]] for d in range(DIAS_P)])
    lf = linha(f"{DIAS_P} dias, 100% flex.", len(sel), exf, c100["custo"])
    print(f"\n2) Semana pequena ({DIAS_P} dias, {POR_DIA} pedidos por dia), todos os clientes aceitam esperar até {folga} dias, dia de entrega livre:")
    mostra(lf)
    # mesma semana pequena, tudo no mesmo dia (para ver quanto o exato ganha só por escolher o dia)
    exm = resolver_enum(I, P, [[i for i in sel if I["dia"][i] == d] for d in range(DIAS_P)])
    lm = linha(f"{DIAS_P} dias, mesmo dia", len(sel), exm, res2["politicas"][1]["custo"])
    mostra(lm)

    # 3) conferência cruzada: os dois métodos exatos numa instância bem pequena (o big-M só prova coisa pequena)
    mini = por_dia[0][:7]
    I1 = dict(I, eco=None)   # o modelo big-M só tem um caminho por par: a conferência usa só o caminho mais rápido
    e1, e2 = resolver_enum(I1, P, [mini]), resolver_milp(I1, P, [mini], LIMITE_S)
    confere = {"pedidos": len(mini), "enumeracao": e1["custo"], "big_m": e2["custo"], "big_m_provado": e2["otimo_provado"],
               "tempo_enumeracao": e1["tempo"], "tempo_big_m": e2["tempo"]}
    print(f"\n3) Conferência dos dois métodos exatos em {len(mini)} pedidos do dia 1: enumeração R$ {e1['custo']:.2f} ({e1['tempo']:.1f}s) x "
          f"big-M R$ {e2['custo']:.2f} ({e2['tempo']:.1f}s, {'provado' if e2['otimo_provado'] else 'NÃO provado'})")
    if e2["otimo_provado"]:
        assert abs(e1["custo"] - e2["custo"]) < 0.05, "os dois métodos exatos discordam: há bug em um deles"

    negativos = [l["nome"] for l in mesmo_dia + [lf, lm] if l["gap_pct"] is not None and l["gap_pct"] < -1e-4 and l["otimo_provado"]]
    json.dump({"limite_s": LIMITE_S, "mesmo_dia": mesmo_dia, "semana": semana, "flexivel_pequena": lf, "mesmo_dia_pequena": lm, "conferencia": confere},
              open(RAIZ / "saida/exato_frota.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=lambda o: o.item())
    print("\ngap = quanto o plano do programa ficou acima do ótimo (0% = achou o ótimo). Detalhes em saida/exato_frota.json")
    if negativos:
        print(f"ATENÇÃO: gap NEGATIVO em {negativos}: o modelo exato está cortando soluções válidas. Isso é bug.")
        sys.exit(2)


if __name__ == "__main__":
    main_semanas() if MODO_SEMANAS else main()
