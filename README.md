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
```

## Requisitos
Python 3 (só biblioteca padrão) e Rust (`cargo`). Para o OSRM local: Docker.

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
| B) Otimizado, mesmo dia | mesma regra de prazo, rotas otimizadas (Clarke-Wright + busca local + ILS) |
| C) X% flexível | X% dos pedidos aceitam até 2 dias de espera; o solver escolhe o dia e a rota |

A curva C mostra **quanto custa a meta de entregar no mesmo dia**.

## Premissas (topo de `solver/src/main.rs`)
Caminhão de 12 t, jornada de 13 h (sem pernoite), frota de 5 caminhões, diesel a R$ 7,31/L,
motorista a R$ 30/h, R$ 180 por caminhão usado no dia. O OSRM usa perfil de carro, então o tempo é
multiplicado por 1,25. O consumo sobe em trechos lentos (terra, serra) e com o caminhão carregado.
Pedidos que não cabem numa jornada nem sozinhos (ex.: Parnaíba) ficam de fora e aparecem em aviso.

## Modelo exato
`scripts/exato.py` confere a heurística contra o **ótimo provado** em instâncias pequenas. Ele pega os
primeiros N pedidos de `dados/` (só os que cabem numa jornada), monta o roteamento de um dia como
programação linear inteira (fluxo de carga + tempo acumulado, consumo crescendo com a carga) e resolve com o
HiGHS (`scipy.optimize.milp`, limite de 300 s por instância). Para isolar o roteamento, frota livre e sem
custo fixo por caminhão: o solver Rust roda no mesmo subconjunto com `FROTA=999 CAMINHAO_RS_DIA=0`
(variáveis de ambiente que sobrescrevem as premissas). A tabela mostra o custo exato, o da heurística,
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
