"""Compara as matrizes de caminhão (dados/referencia) com as antigas de carro (dados/referencia_carro).

Antes: OSRM público, perfil de carro, tempo x 1,25, consumo estimado pela velocidade média do par.
Agora: OSRM local, perfil de caminhão, consumo somado trecho a trecho, dois caminhos por par.
Saída: saida/comparacao_caminhao.json
"""
import json, tomllib
from pathlib import Path
import numpy as np

RAIZ = Path(__file__).resolve().parent.parent
ler = lambda arq: np.array([[float(x) for x in l.split(",")] for l in open(arq) if l.strip()])


def main():
    with open(RAIZ / "dados/premissas.toml", "rb") as f:
        t = tomllib.load(f)
    co, fator = t["consumo"], t["jornada"]["fator_caminhao"]
    A, N = RAIZ / "dados/referencia_carro", RAIZ / "dados/referencia"
    kc, hc0 = ler(A / "matriz_km.csv"), ler(A / "matriz_h.csv")
    k, h, l = ler(N / "matriz_km.csv"), ler(N / "matriz_h.csv"), ler(N / "matriz_l.csv")
    ke, he, le = ler(N / "matriz_km_eco.csv"), ler(N / "matriz_h_eco.csv"), ler(N / "matriz_l_eco.csv")
    m = ~np.eye(len(k), dtype=bool)
    with np.errstate(divide="ignore", invalid="ignore"):
        v = np.where(hc0 > 0, kc / hc0, 60.0)
    lc = kc * co["consumo_base_l_km"] * (1 + co["consumo_lento_extra"] * np.clip((co["vel_ref_kmh"] - v) / co["vel_ref_kmh"], 0, None))
    hc = hc0 * fator
    eco_dif = m & (le < l - 0.05) & (np.abs(ke - k) > 0.05)
    antes = json.loads((A / "esperado.json").read_text(encoding="utf-8"))
    agora = json.loads((N / "esperado.json").read_text(encoding="utf-8"))
    out = {"pares": int(m.sum()),
           "pares_caminho_mudou": int((np.abs(k[m] - kc[m]) > 1).sum()),
           "tempo_razao_media": round(float((h[m] / hc[m]).mean()), 3),
           "litros_razao_media": round(float((l[m] / lc[m]).mean()), 3),
           "consumo_l_km_antes": round(float(lc[m].sum() / kc[m].sum()), 3), "consumo_l_km_agora": round(float(l[m].sum() / k[m].sum()), 3),
           "eco_pares_diferentes": int(eco_dif.sum()),
           "eco_litros_poupados_medio": round(float((l - le)[eco_dif].mean()), 2), "eco_minutos_a_mais_medio": round(float((he - h)[eco_dif].mean() * 60), 1),
           "custos": [{"nome": a["nome"], "antes": a["custo"], "agora": next(x["custo"] for x in agora if x["nome"] == a["nome"])} for a in antes]}
    json.dump(out, open(RAIZ / "saida/comparacao_caminhao.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
