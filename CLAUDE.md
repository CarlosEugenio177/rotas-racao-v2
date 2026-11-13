# Contexto para o Claude Code
Simulação de roteirização (VRP multi-dia) para um distribuidor de ração animal em Teresina-PI.
- Pipeline: `./run.sh [URL_OSRM]` (gera pedidos -> matriz OSRM -> solver Rust -> mapa).
- Teste offline: `./run_mock.sh`.
- Premissas de custo e frota: constantes no topo de `solver/src/main.rs`.
- O solver valida as soluções com asserts (pedido entregue 1x, janela, frota, jornada); mantenha isso ao mudar o modelo.
- Ao trocar pedidos por dados reais, não versionar dados de clientes.
