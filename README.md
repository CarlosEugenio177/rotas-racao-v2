# Roteirização de entregas de ração — simulação v2

Simulação de entregas intermunicipais no Piauí com **distâncias reais de estrada** (OSRM sobre OpenStreetMap),
vários dias, frota limitada e pedidos que precisam sair no mesmo dia ou que aceitam esperar.

```
dados/cidades.csv       cidades reais (coordenadas aproximadas) e peso na sorte dos pedidos
scripts/gerar_pedidos.py  semana de pedidos fictícios de fazendas perto dessas cidades
scripts/osrm_matriz.py    matriz real de km e tempo entre depósito e fazendas (OSRM /table)
solver/                   otimizador em Rust (sem dependências)
scripts/mapa.py           geometria real das viagens (OSRM /route) + saida/mapa.html (Leaflet)
scripts/mock_osrm.py      OSRM falso para testar sem internet
scripts/exato.py          modelo exato (programação inteira) x heurística, em instâncias pequenas
scripts/gerar_fazendas.py  200 fazendas fixas para sortear várias semanas (dados/semanas/)
scripts/semanas.py        roda as políticas em 20 semanas sorteadas -> saida/semanas.json
scripts/pareto.py         tempo x diesel: planos mais rápido, mais econômico e equilibrado -> saida/pareto.json
scripts/exato_frota.py    ótimo provado com frota e custo por caminhão x solver -> saida/exato_frota.json
scripts/regressao.sh      confere se os custos na instância de referência continuam os mesmos
dados/premissas.toml      capacidade, jornada, frota, preços e consumo (solver e exato.py leem daqui)
dados/referencia/         cópia congelada dos dados + custos esperados (esperado.json)
```

## Rotas de caminhão (roteador local)
A partir da Fase 5 as matrizes vêm de dois roteadores OSRM **locais** com perfil de caminhão, e não mais do
servidor público com perfil de carro:

```bash
scripts/osrm_local.sh                 # baixa o mapa do Nordeste (445 MB), recorta, processa e sobe as portas 5001 e 5002 (~6 min)
python3 scripts/matriz_caminhao.py    # matrizes de dados/pedidos.csv -> dados/matriz_{km,h,l}[_eco].csv (~20 s)
python3 scripts/matriz_caminhao.py dados/semanas/fazendas.csv dados/semanas   # as 200 fazendas (~4 min)
scripts/osrm_local.sh parar
```
- `perfis/caminhao.lua` (caminho mais rápido) e `perfis/caminhao_eco.lua` (menor consumo) são gerados por
  `scripts/perfil_caminhao.py` a partir do `car.lua` do OSRM, com as premissas de `[perfil_caminhao]` em
  `dados/premissas.toml`: tamanho e peso do veículo, 80% da velocidade do carro em cada trecho, teto de 80 km/h.
- O tempo já é de caminhão (o fator 1,25 só vale para matrizes de carro, sem `matriz_l.csv`).
- O consumo é somado trecho a trecho da rota (`matriz_l.csv`, caminhão vazio); o solver aplica o efeito da carga.
- Com as matrizes `_eco`, cada trecho usa o caminho de menor custo para a carga do momento.
- `scripts/comparar_matrizes.py` compara com as matrizes antigas de carro, guardadas em `dados/referencia_carro/`
  e `dados/semanas_carro/`.
- O `run.sh` continua funcionando com o OSRM público (perfil de carro), mas gera matrizes do modelo antigo.

## Requisitos
Python 3 e Rust (`cargo`). Para o OSRM local: Docker. Para os modelos exatos de conferência: scipy.

## Rodar

**Rápido (servidor público de demonstração do OSRM):**
```bash
./run.sh
```
Leva 2–3 minutos, porque o script espera 1 s entre chamadas para respeitar o servidor público.
Depois, abra `saida/mapa.html`.

**Recomendado (OSRM local, sem limites):**
```bash
mkdir -p osrm && cd osrm
wget https://download.geofabrik.de/south-america/brazil/nordeste-latest.osm.pbf
docker run -t -v "$PWD:/data" ghcr.io/project-osrm/osrm-backend osrm-extract -p /opt/car.lua /data/nordeste-latest.osm.pbf
docker run -t -v "$PWD:/data" ghcr.io/project-osrm/osrm-backend osrm-partition /data/nordeste-latest.osrm
docker run -t -v "$PWD:/data" ghcr.io/project-osrm/osrm-backend osrm-customize /data/nordeste-latest.osrm
docker run -d -p 5000:5000 -v "$PWD:/data" ghcr.io/project-osrm/osrm-backend osrm-routed --algorithm mld --max-table-size 1000 /data/nordeste-latest.osrm
cd .. && ./run.sh http://localhost:5000
```

**Sem internet (só para testar o código):** `./run_mock.sh`

## O que o solver compara
| Política | O que é |
|---|---|
| A) Manual | tudo entregue no dia do pedido, sempre indo no cliente mais perto (o jeito de hoje) |
| A2) Manual por região | tudo no dia do pedido; pedidos varridos por direção a partir de Teresina, um caminhão por região, do mais perto ao mais longe ou o contrário (despacho manual bem feito) |
| B) Otimizado, mesmo dia | mesma regra de prazo; ótimo provado por dia (programação dinâmica) em dias de até 16 pedidos, senão Clarke-Wright + busca local + ILS |
| C) X% flexível | X% dos pedidos aceitam até 2 dias de espera; o solver escolhe o dia e a rota |

A curva C mostra **quanto custa a meta de entregar no mesmo dia**.

A economia é medida contra a linha de base **mais forte** (a mais barata na média das semanas simuladas;
hoje, a A2). Na página, a outra aparece só como comparação.

## Premissas (`dados/premissas.toml`)
Caminhão de 12 t, jornada de 13 h (sem pernoite), frota de 5 caminhões, diesel a R$ 7,31/L,
motorista a R$ 30/h, R$ 180 por caminhão usado no dia. Rotas com perfil de caminhão (80% da velocidade
do carro em cada trecho). O consumo sobe em trechos lentos (terra, serra) e com o caminhão carregado.
Nenhum desses números foi calibrado com dados reais.
Pedidos que não cabem numa jornada nem sozinhos (ex.: Parnaíba) ficam de fora e aparecem em aviso.

## Teste de regressão
```bash
scripts/regressao.sh               # roda o solver em dados/referencia/ e compara com esperado.json (tolerância R$ 0,01)
scripts/regressao.sh --atualizar   # só quando o modelo de custo mudou de propósito; explique no commit
```
O `run.sh` sorteia pedidos novos em `dados/pedidos.csv`; a instância de `dados/referencia/` não muda.

## Várias semanas (a economia é robusta?)
Uma semana sorteada pode ser sorte. `scripts/semanas.py` sorteia 20 semanas (seeds 1 a 20; fracas, normais
e fortes) a partir de um conjunto fixo de 200 fazendas fictícias e roda todas as políticas em cada uma.
Grava `saida/semanas.json`, que a página usa na manchete ("economia de X%, entre Y% e Z%").

```bash
python3 scripts/gerar_fazendas.py     # só se quiser outro conjunto: grava dados/semanas/fazendas.csv
OSRM_BLOCO=40 python3 scripts/osrm_matriz.py https://router.project-osrm.org dados/semanas/fazendas.csv dados/semanas
python3 scripts/semanas.py            # ~10 s; não chama o OSRM
python3 scripts/mapa.py
```
As faixas de pedidos por dia de cada tipo de semana são premissas (topo do `semanas.py`). Quando o jeito de
hoje não cabe na frota em algum dia, a semana é comparada com frota livre e fica marcada no resultado.

## Tempo x combustível
O custo em R$ soma diesel e hora de motorista. `scripts/pareto.py` roda a instância de referência com 21 pesos
entre "só diesel" e "só tempo" (variáveis de ambiente `PESO_DIESEL` e `PESO_TEMPO`, que mudam só a busca; os
custos do relatório continuam em R$ reais), descarta os planos dominados e nomeia três: mais rápido, mais
econômico e equilibrado (menor R$). Repete os extremos nas 20 semanas. Grava `saida/pareto.json`.

```bash
python3 scripts/pareto.py    # ~40 s
```

## Ótimo provado com frota (`scripts/exato_frota.py`)
Resolve de forma exata o problema completo de um dia: várias viagens por caminhão, jornada, frota limitada e
custo por caminhão usado. Enumera todas as viagens e todos os "dias de caminhão" possíveis (programação
dinâmica sobre subconjuntos) e escolhe a melhor partição no HiGHS. Compara com o solver Rust, que para a
política B usa o mesmo tipo de método (implementado à parte, em Rust) em dias de até 16 pedidos.

```bash
python3 scripts/exato_frota.py            # 5 dias da referência + semana pequena com dia de entrega livre
python3 scripts/exato_frota.py --semanas  # 100 dias e 20 semanas pequenas das semanas simuladas (~1 min)
```
Gap negativo é bug: o script termina com erro se o solver sair mais barato que o "ótimo".

Variáveis de ambiente do solver para experimentos: `ILS_ITERS` (padrão 1000), `BLOCOS=1` (movimentos de bloco
na busca local; testados na Fase 4 e desligados porque não melhoraram o resultado), `EXATO_DIA=0` (desliga o
método exato por dia), `SO_MESMO_DIA=1` (pula as políticas com espera).

## Modelo exato (roteamento puro, sem frota)
`scripts/exato.py` confere a heurística contra o **ótimo provado** em instâncias pequenas. Ele pega os
primeiros N pedidos de `dados/` (só os que cabem numa jornada), monta o roteamento de um dia como
programação linear inteira (fluxo de carga + tempo acumulado, consumo crescendo com a carga) e resolve com o
HiGHS (`scipy.optimize.milp`, limite de 300 s por instância). Para isolar o roteamento, frota livre e sem
custo fixo por caminhão: o solver Rust roda no mesmo subconjunto com `FROTA=999 CAMINHAO_RS_DIA=0`
(variáveis de ambiente que sobrescrevem `dados/premissas.toml`). A tabela mostra o custo exato, o da heurística,
o gap entre os dois e o limite inferior da relaxação linear; detalhes em `saida/exato.json`.

```bash
sudo apt install -y python3-scipy       # ou: pip install "scipy>=1.9"
python3 scripts/exato.py                # tamanhos 6, 8, 10, 12, 14, 16
python3 scripts/exato.py 20             # só um tamanho (tende a bater no limite de tempo)
```

## Usar dados reais
Substitua `dados/pedidos.csv` (colunas `id,nome,lat,lon,demanda_t,dia,u`; `u` é um número entre 0 e 1
usado para sortear os flexíveis) e pule o passo 1 do `run.sh`.

## Próximos passos
- Perfil de **caminhão** de verdade: Valhalla (`costing=truck`) ou GraphHopper, com restrição de peso e altura.
- **Pernoite** e viagens de vários dias para destinos distantes (Parnaíba, Picos).
- Janela de **horário** do cliente e mais de um depósito.
- Comparar com solvers prontos: `vrp-pragmatic` (Rust) ou OR-Tools (Python).
