"""Gera os dois perfis de caminhão do OSRM a partir do car.lua original da imagem do OSRM.

  perfis/caminhao.lua      caminho MAIS RÁPIDO para caminhão (peso = duração)
  perfis/caminhao_eco.lua  caminho de MENOR CONSUMO para caminhão vazio (peso = diesel, em ml)

Mudanças em relação ao carro (todas premissas nossas, ver dados/premissas.toml [perfil_caminhao]):
  - velocidade: a do carro em cada trecho vezes fator_vel_caminhao, com teto vel_max_caminhao_kmh
    (é a antiga premissa "tempo x 1,25", agora dentro do perfil e aplicada trecho a trecho);
  - altura, largura, comprimento e peso do veículo (o OSRM usa para respeitar maxheight, maxweight etc.);
  - no perfil econômico, o peso de cada trecho é o consumo: litros/km = base * (1 + extra * max(0, (vref - v) / vref)),
    onde v é a velocidade do caminhão naquele trecho. É a mesma fórmula que scripts/matriz_caminhao.py aplica
    trecho a trecho para calcular os litros de cada rota.

Uso: python3 scripts/perfil_caminhao.py     (perfis/car_original.lua já tem que existir; ver scripts/osrm_local.sh)
"""
import re, tomllib
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def troca(s, a, b, n=1):
    assert s.count(a) == n, f"esperava {n}x no car.lua: {a!r} (achei {s.count(a)}); a versão do OSRM mudou?"
    return s.replace(a, b)


def gerar(eco):
    with open(RAIZ / "dados/premissas.toml", "rb") as f:
        t = tomllib.load(f)
    pc, co = t["perfil_caminhao"], t["consumo"]
    s = (RAIZ / "perfis/car_original.lua").read_text(encoding="utf-8")
    s = troca(s, "      weight_name                     = 'routability',",
              f"      weight_name                     = '{'consumo' if eco else 'duration'}',")
    s = troca(s, "vehicle_height = 2.0,", f"vehicle_height = {pc['altura_m']},")
    s = troca(s, "vehicle_width = 1.9,", f"vehicle_width = {pc['largura_m']},")
    s = troca(s, "vehicle_length = 4.8,", f"vehicle_length = {pc['comprimento_m']},")
    s = troca(s, "vehicle_weight = 2000,", f"vehicle_weight = {int(pc['peso_bruto_kg'])},")
    # Velocidade: a do carro em cada trecho (tipo de via, maxspeed, pavimento) vezes o fator do caminhão, com teto.
    s = troca(s, "  WayHandlers.run(profile, way, result, data, handlers, relations)\n",
              "  WayHandlers.run(profile, way, result, data, handlers, relations)\n"
              "\n  -- CAMINHAO: fracao da velocidade do carro em cada trecho, com teto (dados/premissas.toml)\n"
              f"  if result.forward_speed > 0 then\n    result.forward_speed = math.min(result.forward_speed * {pc['fator_vel_caminhao']}, {pc['vel_max_caminhao_kmh']})\n  end\n"
              f"  if result.backward_speed > 0 then\n    result.backward_speed = math.min(result.backward_speed * {pc['fator_vel_caminhao']}, {pc['vel_max_caminhao_kmh']})\n  end\n")
    if eco:
        s = troca(s, "  if result.backward_speed > 0 then\n    result.backward_speed = math.min(",
                  "  -- (o bloco do perfil economico vem logo depois do ajuste de velocidade)\n  if result.backward_speed > 0 then\n    result.backward_speed = math.min(")
        fim_bloco = f"{pc['vel_max_caminhao_kmh']})\n  end\n"
        assert s.count(fim_bloco) == 2
        pos = s.rindex(fim_bloco) + len(fim_bloco)
        s = s[:pos] + ("\n  -- PERFIL ECONOMICO: o peso do trecho e o consumo de diesel do caminhao vazio, em ml.\n"
                  "  -- rate = metros por unidade de peso = 1 / (litros por km), porque litros/km = ml/m.\n"
                  f"  local function rate_consumo(v)\n    local c = {co['consumo_base_l_km']} * (1 + {co['consumo_lento_extra']} * math.max(0, ({pc['vel_ref_caminhao_kmh']} - v) / {pc['vel_ref_caminhao_kmh']}))\n    return 1 / c\n  end\n"
                  "  if result.forward_mode ~= mode.inaccessible and result.forward_speed > 0 then\n    result.forward_rate = rate_consumo(result.forward_speed)\n  end\n"
                  "  if result.backward_mode ~= mode.inaccessible and result.backward_speed > 0 then\n    result.backward_rate = rate_consumo(result.backward_speed)\n  end\n") + s[pos:]
        s = troca(s, "  if profile.properties.weight_name == 'distance' then\n     turn.weight = 0",
                  "  if profile.properties.weight_name == 'distance' or profile.properties.weight_name == 'consumo' then\n     turn.weight = 0")
    cab = f"-- GERADO por scripts/perfil_caminhao.py a partir de perfis/car_original.lua. Nao edite a mao.\n-- Perfil de caminhao: {'menor consumo (peso = diesel em ml)' if eco else 'mais rapido (peso = duracao)'}.\n"
    (RAIZ / "perfis" / ("caminhao_eco.lua" if eco else "caminhao.lua")).write_text(cab + s, encoding="utf-8")


if __name__ == "__main__":
    gerar(False); gerar(True)
    print("perfis/caminhao.lua e perfis/caminhao_eco.lua gerados")
