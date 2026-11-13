# Contexto para o Claude Code
Simulação de roteirização (VRP multi-dia) para um distribuidor de ração animal em Teresina-PI.
- Pipeline: `./run.sh [URL_OSRM]` (gera pedidos -> matriz OSRM -> solver Rust -> mapa).
- Teste offline: `./run_mock.sh`.
- Premissas de custo e frota: `dados/premissas.toml` (lido pelo solver e pelo `scripts/exato.py`). Não ponha premissa no código.
- Antes e depois de qualquer mudança: `scripts/regressao.sh` (instância congelada em `dados/referencia/`). Só use `--atualizar` quando o modelo de custo mudar de propósito, com o motivo no commit.
- Não rode `run.sh` sem necessidade: ele sorteia pedidos novos em `dados/pedidos.csv`.
- A página é gerada: edite `scripts/mapa_template.html`, nunca `saida/mapa.html`.
- Matrizes de estrada: roteador OSRM local com perfil de caminhão (`scripts/osrm_local.sh` + `scripts/matriz_caminhao.py`). Neste WSL o Docker só funciona pelo `docker.exe` do Windows; o script já trata disso.
- Depois de mudar o solver ou as matrizes, regenere: `semanas.py`, `pareto.py`, `exato_frota.py` (e `--semanas`), `comparar_matrizes.py`, `mapa.py`.
- O solver valida as soluções com asserts (pedido entregue 1x, janela, frota, jornada); mantenha isso ao mudar o modelo.
- Ao trocar pedidos por dados reais, não versionar dados de clientes.
