#!/usr/bin/env bash
# Teste de regressão: roda o solver na instância de referência congelada (dados/referencia/)
# com as premissas atuais (dados/premissas.toml) e compara o custo de cada política com
# dados/referencia/esperado.json (tolerância de R$ 0,01).
#
# Uso: scripts/regressao.sh              confere
#      scripts/regressao.sh --atualizar  grava os custos atuais como esperados
#                                        (só quando o modelo de custo mudou DE PROPÓSITO;
#                                         explique o motivo no commit)
set -euo pipefail
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/dados"
cp "$RAIZ"/dados/referencia/*.csv "$RAIZ/dados/premissas.toml" "$TMP/dados/"
(cd "$RAIZ/solver" && cargo build --release -q)
# sem as variáveis que sobrescrevem premissas; os asserts do solver (pedido 1x, janela, frota, jornada) derrubam aqui se falharem
env -u FROTA -u CAMINHAO_RS_DIA "$RAIZ/solver/target/release/solver" "$TMP" > "$TMP/saida.txt" || { cat "$TMP/saida.txt"; echo "FALHOU: o solver terminou com erro"; exit 1; }
python3 - "$TMP/saida/resultado.json" "$RAIZ/dados/referencia/esperado.json" "${1:-}" <<'PY'
import json, sys
res, arq, modo = sys.argv[1], sys.argv[2], sys.argv[3]
r = json.load(open(res, encoding="utf-8"))
atual = [{"nome": p["nome"], "custo": p["custo"]} for p in r["politicas"] + [r["base_regiao"]]]
if modo == "--atualizar":
    json.dump(atual, open(arq, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"Valores esperados atualizados ({len(atual)} políticas) em {arq}")
    sys.exit(0)
esp = json.load(open(arq, encoding="utf-8"))
ok = len(esp) == len(atual)
if not ok:
    print(f"número de políticas mudou: esperado {len(esp)}, veio {len(atual)}")
for e, a in zip(esp, atual):
    dif = a["custo"] - e["custo"]
    bom = e["nome"] == a["nome"] and abs(dif) <= 0.01 + 1e-9
    ok &= bom
    print(f"{'ok    ' if bom else 'DIFERE'} {a['nome']:<44} esperado {e['custo']:>10.2f}  atual {a['custo']:>10.2f}  dif {dif:+.2f}")
print("REGRESSÃO OK" if ok else "REGRESSÃO FALHOU: se a mudança foi de propósito, rode scripts/regressao.sh --atualizar e explique no commit")
sys.exit(0 if ok else 1)
PY
