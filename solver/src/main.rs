//! Solver multi-dia de roteirização de ração (v2).
//!
//! Entrada (gerada pelos scripts Python):
//!   dados/pedidos.csv   id,nome,lat,lon,demanda_t,dia,u
//!   dados/matriz_km.csv / dados/matriz_h.csv   (OSRM real; ponto 0 = depósito, i = pedido i)
//!   dados/premissas.toml   capacidade, jornada, frota, preços e consumo
//!
//! Modelo:
//!   - Horizonte de DIAS dias. Cada pedido chega num dia e tem uma janela [dia, dia+folga].
//!     Folga 0 = "tem que sair no mesmo dia".
//!   - Frota limitada de caminhões/dia. Cada caminhão faz várias viagens no dia, desde que a soma
//!     caiba na jornada (empacotamento First-Fit Decreasing das viagens nos caminhões).
//!   - Custo = diesel (depende da velocidade média do trecho e da carga) + hora motorista
//!     + custo fixo por caminhão usado no dia.
//!
//! Políticas comparadas:
//!   A) Manual: tudo no dia do pedido, rota "vai no mais perto" (o que se faz hoje)
//!   A2) Manual por região: tudo no dia do pedido, caminhões enchidos por direção a partir do depósito
//!   B) Otimizado, tudo no mesmo dia
//!   C) Otimizado com X% dos pedidos aceitando até folga_dias de espera (curva 0..100%)

use std::fmt::Write as _;
use std::fs;
use std::path::PathBuf;
use std::sync::OnceLock;

// ---------------- Premissas ----------------
// Vêm de dados/premissas.toml (o mesmo arquivo que o scripts/exato.py lê). Aqui só ficam
// os parâmetros do algoritmo, que não mudam o modelo de custo.
struct Premissas {
    capacidade_t: f64,
    jornada_h: f64,
    descarga_h: f64,
    carregar_h: f64,
    fator_caminhao: f64,
    diesel_rs_l: f64,
    motorista_rs_h: f64,
    caminhao_rs_dia: f64,
    consumo_base_l_km: f64,
    alpha_carga: f64,
    consumo_lento_extra: f64,
    vel_ref_kmh: f64,
    frota: usize,
    folga_dias: usize,
    deposito_lat: f64,
    deposito_lon: f64,
    // Pesos da BUSCA (não são premissas de custo): quanto o diesel e o tempo contam na hora de
    // escolher o plano. 1 e 1 = custo real em R$. Variáveis de ambiente PESO_DIESEL e PESO_TEMPO.
    peso_diesel: f64,
    peso_tempo: f64,
}

static PREM: OnceLock<Premissas> = OnceLock::new();

fn p() -> &'static Premissas { PREM.get().expect("premissas não carregadas") }

/// Lê dados/premissas.toml: linhas `chave = número`, com `# comentário` e `[seção]` ignorados.
/// Chave faltando ou desconhecida é erro (nada de valor padrão escondido no código).
/// As variáveis de ambiente CAMINHAO_RS_DIA e FROTA sobrescrevem o arquivo (usado pelo exato.py).
fn ler_premissas(raiz: &PathBuf) -> Premissas {
    let arq = raiz.join("dados/premissas.toml");
    let txt = fs::read_to_string(&arq).unwrap_or_else(|_| panic!("não achei {:?}", arq));
    let mut v: Vec<(String, f64)> = vec![];
    for l in txt.lines() {
        let l = l.split('#').next().unwrap().trim();
        if l.is_empty() || l.starts_with('[') { continue; }
        let (k, x) = l.split_once('=').unwrap_or_else(|| panic!("premissas.toml: linha inválida: {l}"));
        let x: f64 = x.trim().parse().unwrap_or_else(|_| panic!("premissas.toml: valor inválido em: {l}"));
        v.push((k.trim().to_string(), x));
    }
    let mut pega = |k: &str| -> f64 {
        let i = v.iter().position(|(c, _)| c == k).unwrap_or_else(|| panic!("premissas.toml: falta a chave {k}"));
        v.remove(i).1
    };
    let inteiro = |k: &str, x: f64| -> usize {
        assert!(x >= 0.0 && x.fract() == 0.0, "premissas: {k} tem que ser inteiro, veio {x}");
        x as usize
    };
    let env = |k: &str| std::env::var(k).ok().map(|s| s.parse::<f64>().unwrap_or_else(|_| panic!("variável {k} inválida: {s}")));
    let pr = Premissas {
        capacidade_t: pega("capacidade_t"),
        jornada_h: pega("jornada_h"),
        descarga_h: pega("descarga_h"),
        carregar_h: pega("carregar_h"),
        fator_caminhao: pega("fator_caminhao"),
        diesel_rs_l: pega("diesel_rs_l"),
        motorista_rs_h: pega("motorista_rs_h"),
        caminhao_rs_dia: { let a = pega("caminhao_rs_dia"); env("CAMINHAO_RS_DIA").unwrap_or(a) },
        consumo_base_l_km: pega("consumo_base_l_km"),
        alpha_carga: pega("alpha_carga"),
        consumo_lento_extra: pega("consumo_lento_extra"),
        vel_ref_kmh: pega("vel_ref_kmh"),
        frota: { let a = pega("frota"); inteiro("frota", env("FROTA").unwrap_or(a)) },
        folga_dias: { let a = pega("folga_dias"); inteiro("folga_dias", a) },
        deposito_lat: pega("deposito_lat"),
        deposito_lon: pega("deposito_lon"),
        peso_diesel: env("PESO_DIESEL").unwrap_or(1.0),
        peso_tempo: env("PESO_TEMPO").unwrap_or(1.0),
    };
    assert!(v.is_empty(), "premissas.toml: chave desconhecida: {}", v[0].0);
    pr
}

const ILS_ITERS: usize = 300;
const PENALIDADE: f64 = 50_000.0;

// ---------------- Utilidades ----------------
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    fn f(&mut self) -> f64 { (self.next() % 1_000_000) as f64 / 1e6 }
    fn range(&mut self, n: usize) -> usize { (self.next() % n.max(1) as u64) as usize }
}

fn ler_matriz(p: &PathBuf) -> Vec<Vec<f64>> {
    fs::read_to_string(p)
        .unwrap_or_else(|_| panic!("não achei {:?} — rode scripts/osrm_matriz.py antes", p))
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| l.split(',').map(|x| x.trim().parse().unwrap()).collect())
        .collect()
}

#[derive(Clone)]
struct Pedido {
    nome: String,
    lat: f64,
    lon: f64,
    demanda: f64,
    dia: usize,
    u: f64,
}

struct Inst {
    ped: Vec<Pedido>, // índice 0 = depósito (vazio)
    litros_vazio: Vec<Vec<f64>>,
    h_cam: Vec<Vec<f64>>,
    km: Vec<Vec<f64>>,
    dias: usize,
    janela: Vec<(usize, usize)>,
    ativo: Vec<bool>, // false = não cabe numa jornada nem sozinho (precisa pernoite)
}

fn consumo_l_km(vel_kmh: f64) -> f64 {
    // estrada lenta (terra, serra, cidade) gasta mais por km
    let p = p();
    p.consumo_base_l_km * (1.0 + p.consumo_lento_extra * ((p.vel_ref_kmh - vel_kmh) / p.vel_ref_kmh).max(0.0))
}

fn carregar(raiz: &PathBuf) -> Inst {
    let txt = fs::read_to_string(raiz.join("dados/pedidos.csv")).expect("dados/pedidos.csv");
    let mut ped = vec![Pedido { nome: "Depósito".into(), lat: p().deposito_lat, lon: p().deposito_lon, demanda: 0.0, dia: 0, u: 0.0 }];
    for l in txt.lines().skip(1).filter(|l| !l.trim().is_empty()) {
        let c: Vec<&str> = l.split(',').collect();
        ped.push(Pedido {
            nome: c[1].to_string(),
            lat: c[2].parse().unwrap(),
            lon: c[3].parse().unwrap(),
            demanda: c[4].parse().unwrap(),
            dia: c[5].parse().unwrap(),
            u: c[6].parse().unwrap(),
        });
    }
    let km = ler_matriz(&raiz.join("dados/matriz_km.csv"));
    let h = ler_matriz(&raiz.join("dados/matriz_h.csv"));
    assert_eq!(km.len(), ped.len(), "matriz e pedidos com tamanhos diferentes — rode osrm_matriz.py de novo");
    let n = ped.len();
    let mut litros = vec![vec![0.0; n]; n];
    let mut hc = vec![vec![0.0; n]; n];
    for i in 0..n {
        for j in 0..n {
            if i == j { continue; }
            let v = if h[i][j] > 0.0 { km[i][j] / h[i][j] } else { 60.0 };
            litros[i][j] = km[i][j] * consumo_l_km(v);
            hc[i][j] = h[i][j] * p().fator_caminhao;
        }
    }
    let dias = ped.iter().map(|p| p.dia).max().unwrap() + 1;
    let mut inst = Inst { janela: vec![(0, 0); n], ativo: vec![true; n], ped, litros_vazio: litros, h_cam: hc, km, dias };
    inst.ativo[0] = false;
    for i in 1..n {
        let a = avalia_viagem(&inst, &[i]);
        if a.h > p().jornada_h || inst.ped[i].demanda > p().capacidade_t {
            inst.ativo[i] = false;
        }
    }
    inst
}

fn define_janelas(inst: &mut Inst, frac_flex: f64) {
    for i in 1..inst.ped.len() {
        let d = inst.ped[i].dia;
        let folga = if inst.ped[i].u < frac_flex { p().folga_dias } else { 0 };
        inst.janela[i] = (d, (d + folga).min(inst.dias - 1));
    }
}

// ---------------- Avaliação ----------------
#[derive(Clone, Copy, Default)]
struct Av {
    km: f64,
    h: f64,
    litros: f64,
    carga: f64,
    custo: f64,
    ok: bool,
}

fn avalia_viagem(inst: &Inst, v: &[usize]) -> Av {
    if v.is_empty() { return Av { ok: true, ..Default::default() }; }
    let p = p();
    let carga0: f64 = v.iter().map(|&i| inst.ped[i].demanda).sum();
    let mut a = Av { carga: carga0, h: p.carregar_h, ..Default::default() };
    let mut carga = carga0;
    let mut prev = 0;
    for &i in v.iter().chain(std::iter::once(&0)) {
        a.km += inst.km[prev][i];
        a.h += inst.h_cam[prev][i];
        a.litros += inst.litros_vazio[prev][i] * (1.0 + p.alpha_carga * carga / p.capacidade_t);
        if i != 0 {
            a.h += p.descarga_h;
            carga -= inst.ped[i].demanda;
        }
        prev = i;
    }
    // custo usado pela busca; com pesos 1 e 1 é o custo real em R$
    a.custo = p.peso_diesel * (a.litros * p.diesel_rs_l) + p.peso_tempo * (a.h * p.motorista_rs_h);
    a.ok = carga0 <= p.capacidade_t + 1e-9 && a.h <= p.jornada_h + 1e-9;
    a
}

/// Empacota as viagens do dia nos caminhões (FFD). Devolve nº de caminhões.
fn caminhoes_no_dia(horas: &mut Vec<f64>) -> usize {
    let jornada = p().jornada_h;
    horas.sort_by(|a, b| b.partial_cmp(a).unwrap());
    let mut bins: Vec<f64> = vec![];
    for &h in horas.iter() {
        match bins.iter_mut().find(|b| **b + h <= jornada + 1e-9) {
            Some(b) => *b += h,
            None => bins.push(h),
        }
    }
    bins.len()
}

fn custo_dia(inst: &Inst, dia: &[Vec<usize>]) -> f64 {
    let mut c = 0.0;
    let mut horas = Vec::with_capacity(dia.len());
    for v in dia {
        if v.is_empty() { continue; }
        let a = avalia_viagem(inst, v);
        c += a.custo;
        if !a.ok { c += PENALIDADE; }
        horas.push(a.h);
    }
    let k = caminhoes_no_dia(&mut horas);
    c += k as f64 * p().caminhao_rs_dia;
    if k > p().frota { c += PENALIDADE * (k - p().frota) as f64; }
    c
}

type Sol = Vec<Vec<Vec<usize>>>; // dia -> viagens -> pedidos

fn custo(inst: &Inst, s: &Sol) -> f64 { s.iter().map(|d| custo_dia(inst, d)).sum() }

/// Custo real de uma viagem em R$ (diesel + motorista), sem os pesos da busca.
fn custo_real_viagem(a: &Av) -> f64 { a.litros * p().diesel_rs_l + a.h * p().motorista_rs_h }

/// Custo real do plano em R$: viagens + custo fixo dos caminhões usados. É o que vai para o relatório.
fn custo_real(inst: &Inst, s: &Sol) -> f64 {
    s.iter().map(|dia| {
        let mut c = 0.0;
        let mut horas = Vec::with_capacity(dia.len());
        for v in dia {
            if v.is_empty() { continue; }
            let a = avalia_viagem(inst, v);
            c += custo_real_viagem(&a);
            horas.push(a.h);
        }
        c + caminhoes_no_dia(&mut horas) as f64 * p().caminhao_rs_dia
    }).sum()
}

// ---------------- Construtivas ----------------
fn pedidos_do_dia(inst: &Inst, d: usize) -> Vec<usize> {
    (1..inst.ped.len()).filter(|&i| inst.ativo[i] && inst.ped[i].dia == d).collect()
}

/// "Vai sempre no mais perto" (km), enchendo o caminhão.
fn manual(inst: &Inst) -> Sol {
    (0..inst.dias)
        .map(|d| {
            let mut falta = pedidos_do_dia(inst, d);
            let mut viagens = vec![];
            while !falta.is_empty() {
                let (mut v, mut atual) = (vec![], 0);
                loop {
                    let mut melhor: Option<(f64, usize)> = None;
                    for (k, &c) in falta.iter().enumerate() {
                        let mut t = v.clone();
                        t.push(c);
                        if !avalia_viagem(inst, &t).ok { continue; }
                        let dist = inst.km[atual][c];
                        if melhor.map_or(true, |(b, _)| dist < b) { melhor = Some((dist, k)); }
                    }
                    match melhor {
                        Some((_, k)) => { atual = falta.remove(k); v.push(atual); }
                        None => break,
                    }
                }
                viagens.push(v);
            }
            viagens
        })
        .collect()
}

/// Direção (ângulo) de cada pedido vista do depósito, para o despacho por região.
fn angulo(inst: &Inst, i: usize) -> f64 {
    let (la0, lo0) = (inst.ped[0].lat, inst.ped[0].lon);
    (inst.ped[i].lat - la0).atan2((inst.ped[i].lon - lo0) * la0.to_radians().cos())
}

/// Ordena as paradas de uma viagem do mais perto para o mais longe do depósito, ou o contrário:
/// o que for viável e mais barato. None se nenhum dos dois couber (carga ou jornada).
fn ordena_perto_longe(inst: &Inst, v: &[usize]) -> Option<Vec<usize>> {
    let mut ida = v.to_vec();
    ida.sort_by(|&a, &b| inst.km[0][a].partial_cmp(&inst.km[0][b]).unwrap());
    let volta: Vec<usize> = ida.iter().rev().cloned().collect();
    let (ai, av) = (avalia_viagem(inst, &ida), avalia_viagem(inst, &volta));
    match (ai.ok, av.ok) {
        (true, true) => Some(if av.custo < ai.custo { volta } else { ida }),
        (true, false) => Some(ida),
        (false, true) => Some(volta),
        _ => None,
    }
}

/// "Despacho por região": o jeito organizado de fazer à mão. Varre os pedidos do dia por direção
/// a partir do depósito (começando depois do maior vão sem pedidos), enche um caminhão com os
/// vizinhos de direção até não caber mais e ordena cada viagem do mais perto ao mais longe ou o
/// contrário. Testa a varredura nos dois sentidos e fica com o dia mais barato.
fn manual_regiao(inst: &Inst) -> Sol {
    (0..inst.dias)
        .map(|d| {
            let mut cli = pedidos_do_dia(inst, d);
            if cli.is_empty() { return vec![]; }
            cli.sort_by(|&a, &b| angulo(inst, a).partial_cmp(&angulo(inst, b)).unwrap());
            let n = cli.len();
            let (mut ini, mut maior) = (0, -1.0);
            for k in 0..n {
                let mut vao = angulo(inst, cli[(k + 1) % n]) - angulo(inst, cli[k]);
                if k + 1 == n { vao += std::f64::consts::TAU; }
                if vao > maior { maior = vao; ini = (k + 1) % n; }
            }
            let ordem: Vec<usize> = (0..n).map(|k| cli[(ini + k) % n]).collect();
            let monta = |ordem: &[usize]| -> Vec<Vec<usize>> {
                let mut viagens = vec![];
                let mut atual: Vec<usize> = vec![];
                for &x in ordem {
                    let mut cand = atual.clone();
                    cand.push(x);
                    match ordena_perto_longe(inst, &cand) {
                        Some(v) => atual = v,
                        None => { viagens.push(atual); atual = vec![x]; }
                    }
                }
                viagens.push(atual);
                viagens
            };
            let ida = monta(&ordem);
            let volta = monta(&ordem.iter().rev().cloned().collect::<Vec<_>>());
            if custo_dia(inst, &volta) < custo_dia(inst, &ida) { volta } else { ida }
        })
        .collect()
}

fn clarke_wright(inst: &Inst, cli: &[usize]) -> Vec<Vec<usize>> {
    let c = |i: usize, j: usize| p().peso_diesel * (inst.litros_vazio[i][j] * p().diesel_rs_l) + p().peso_tempo * (inst.h_cam[i][j] * p().motorista_rs_h);
    let mut sav = vec![];
    for (a, &i) in cli.iter().enumerate() {
        for &j in &cli[a + 1..] {
            sav.push((c(0, i) + c(0, j) - c(i, j), i, j));
        }
    }
    sav.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());
    let mut rotas: Vec<Option<Vec<usize>>> = cli.iter().map(|&i| Some(vec![i])).collect();
    let pos = |x: usize| cli.iter().position(|&y| y == x).unwrap();
    let mut dona: Vec<usize> = (0..cli.len()).collect();
    for (_, i, j) in sav {
        let (ri, rj) = (dona[pos(i)], dona[pos(j)]);
        if ri == rj { continue; }
        let (a, b) = (rotas[ri].clone().unwrap(), rotas[rj].clone().unwrap());
        let mut cands = vec![];
        if a.last() == Some(&i) && b.first() == Some(&j) { cands.push([a.clone(), b.clone()].concat()); }
        if b.last() == Some(&j) && a.first() == Some(&i) { cands.push([b.clone(), a.clone()].concat()); }
        if a.first() == Some(&i) && b.first() == Some(&j) { let mut x = a.clone(); x.reverse(); cands.push([x, b.clone()].concat()); }
        if a.last() == Some(&i) && b.last() == Some(&j) { let mut y = b.clone(); y.reverse(); cands.push([a.clone(), y].concat()); }
        let antes = avalia_viagem(inst, &a).custo + avalia_viagem(inst, &b).custo;
        let mut melhor: Option<(f64, Vec<usize>)> = None;
        for r in cands {
            for rr in [r.clone(), r.iter().rev().cloned().collect::<Vec<_>>()] {
                let av = avalia_viagem(inst, &rr);
                if av.ok && av.custo < antes && melhor.as_ref().map_or(true, |(bc, _)| av.custo < *bc) {
                    melhor = Some((av.custo, rr));
                }
            }
        }
        if let Some((_, r)) = melhor {
            for &x in &r { dona[pos(x)] = ri; }
            rotas[ri] = Some(r);
            rotas[rj] = None;
        }
    }
    rotas.into_iter().flatten().collect()
}

// ---------------- Busca local multi-dia ----------------
fn limpa(s: &mut Sol) {
    for d in s.iter_mut() { d.retain(|v| !v.is_empty()); }
}

/// Custo do dia a partir das avaliações das viagens (sem reavaliar rotas).
fn custo_dia_avs<'a>(avs: impl Iterator<Item = &'a Av>) -> f64 {
    let mut c = 0.0;
    let mut horas = Vec::with_capacity(8);
    for a in avs {
        if a.carga == 0.0 { continue; }
        c += a.custo;
        if !a.ok { c += PENALIDADE; }
        horas.push(a.h);
    }
    let k = caminhoes_no_dia(&mut horas);
    c += k as f64 * p().caminhao_rs_dia;
    if k > p().frota { c += PENALIDADE * (k - p().frota) as f64; }
    c
}

/// Custo do dia trocando algumas viagens (repl) e/ou acrescentando uma nova.
fn custo_dia_mod(avs: &[Av], repl: &[(usize, Av)], extra: Option<Av>) -> f64 {
    let v: Vec<Av> = avs.iter().enumerate()
        .map(|(i, a)| repl.iter().find(|(k, _)| *k == i).map(|(_, b)| *b).unwrap_or(*a))
        .chain(extra)
        .collect();
    custo_dia_avs(v.iter())
}

fn localiza(s: &Sol, x: usize) -> (usize, usize, usize) {
    for (d, dia) in s.iter().enumerate() {
        for (t, v) in dia.iter().enumerate() {
            if let Some(p) = v.iter().position(|&y| y == x) { return (d, t, p); }
        }
    }
    unreachable!()
}

fn busca_local(inst: &Inst, s: &mut Sol) {
    let n = inst.ped.len();
    let mut avs: Vec<Vec<Av>> = s.iter().map(|d| d.iter().map(|v| avalia_viagem(inst, v)).collect()).collect();
    loop {
        let mut melhorou = false;

        // 1) 2-opt dentro de cada viagem
        for d in 0..s.len() {
            let base_dia = custo_dia_avs(avs[d].iter());
            for t in 0..s[d].len() {
                let len = s[d][t].len();
                let mut melhor: Option<(f64, Vec<usize>, Av)> = None;
                for i in 0..len {
                    for j in (i + 1)..len {
                        let mut v = s[d][t].clone();
                        v[i..=j].reverse();
                        let a = avalia_viagem(inst, &v);
                        if !a.ok { continue; }
                        let c = custo_dia_mod(&avs[d], &[(t, a)], None);
                        if c < base_dia - 1e-6 && melhor.as_ref().map_or(true, |(b, _, _)| c < *b) {
                            melhor = Some((c, v, a));
                        }
                    }
                }
                if let Some((_, v, a)) = melhor {
                    s[d][t] = v; avs[d][t] = a; melhorou = true;
                }
            }
        }

        // 2) realocar cada pedido no melhor lugar: qualquer viagem/posição/viagem nova, em qualquer dia da janela
        for x in 1..n {
            if !inst.ativo[x] { continue; }
            let (d, t, p) = localiza(s, x);
            let mut r = s[d][t].clone();
            r.remove(p);
            let av_r = avalia_viagem(inst, &r);
            let base_d = custo_dia_avs(avs[d].iter());
            let (j0, j1) = inst.janela[x];
            // (delta, d2, t2, q, av_nova)
            let mut melhor: Option<(f64, usize, usize, usize, Av)> = None;
            for d2 in j0..=j1 {
                let base_d2 = custo_dia_avs(avs[d2].iter());
                let nt = s[d2].len();
                for t2 in 0..=nt {
                    let alvo: &[usize] = if t2 == nt { &[] } else if d2 == d && t2 == t { &r } else { &s[d2][t2] };
                    for q in 0..=alvo.len() {
                        let mut c = alvo.to_vec();
                        c.insert(q, x);
                        let av_c = avalia_viagem(inst, &c);
                        if !av_c.ok { continue; }
                        let delta = if d2 == d {
                            let novo = if t2 == nt {
                                custo_dia_mod(&avs[d], &[(t, av_r)], Some(av_c))
                            } else if t2 == t {
                                custo_dia_mod(&avs[d], &[(t, av_c)], None)
                            } else {
                                custo_dia_mod(&avs[d], &[(t, av_r), (t2, av_c)], None)
                            };
                            novo - base_d
                        } else {
                            let nd = custo_dia_mod(&avs[d], &[(t, av_r)], None);
                            let nd2 = if t2 == nt { custo_dia_mod(&avs[d2], &[], Some(av_c)) } else { custo_dia_mod(&avs[d2], &[(t2, av_c)], None) };
                            nd + nd2 - base_d - base_d2
                        };
                        if delta < -1e-6 && melhor.as_ref().map_or(true, |m| delta < m.0) {
                            melhor = Some((delta, d2, t2, q, av_c));
                        }
                    }
                }
            }
            if let Some((_, d2, t2, q, av_c)) = melhor {
                s[d][t] = r;
                avs[d][t] = av_r;
                if t2 == s[d2].len() { s[d2].push(vec![x]); avs[d2].push(av_c); }
                else { s[d2][t2].insert(q, x); avs[d2][t2] = av_c; }
                if s[d][t].is_empty() { s[d].remove(t); avs[d].remove(t); }
                melhorou = true;
            }
        }

        // 3) trocar dois pedidos de viagem (mesmo dia ou dias diferentes, respeitando janelas)
        for x in 1..n {
            if !inst.ativo[x] { continue; }
            for y in (x + 1)..n {
                if !inst.ativo[y] { continue; }
                let (d1, t1, p1) = localiza(s, x);
                let (d2, t2, p2) = localiza(s, y);
                if d1 == d2 && t1 == t2 { continue; }
                let (wx, wy) = (inst.janela[x], inst.janela[y]);
                if !(wx.0 <= d2 && d2 <= wx.1 && wy.0 <= d1 && d1 <= wy.1) { continue; }
                let (mut va, mut vb) = (s[d1][t1].clone(), s[d2][t2].clone());
                va[p1] = y; vb[p2] = x;
                let (aa, ab) = (avalia_viagem(inst, &va), avalia_viagem(inst, &vb));
                if !(aa.ok && ab.ok) { continue; }
                let delta = if d1 == d2 {
                    custo_dia_mod(&avs[d1], &[(t1, aa), (t2, ab)], None) - custo_dia_avs(avs[d1].iter())
                } else {
                    custo_dia_mod(&avs[d1], &[(t1, aa)], None) + custo_dia_mod(&avs[d2], &[(t2, ab)], None)
                        - custo_dia_avs(avs[d1].iter()) - custo_dia_avs(avs[d2].iter())
                };
                if delta < -1e-6 {
                    s[d1][t1] = va; s[d2][t2] = vb; avs[d1][t1] = aa; avs[d2][t2] = ab;
                    melhorou = true;
                }
            }
        }
        if !melhorou { break; }
    }
}

fn ils(inst: &Inst, inicial: Sol, iters: usize, seed: u64) -> Sol {
    let mut rng = Rng(seed);
    let mut best = inicial;
    busca_local(inst, &mut best);
    let mut best_c = custo(inst, &best);
    for _ in 0..iters {
        let mut s = best.clone();
        for _ in 0..3 {
            let d = rng.range(s.len());
            if s[d].is_empty() { continue; }
            let t = rng.range(s[d].len());
            if s[d][t].is_empty() { continue; }
            let p = rng.range(s[d][t].len());
            let x = s[d][t].remove(p);
            let (j0, j1) = inst.janela[x];
            let d2 = j0 + rng.range(j1 - j0 + 1);
            if s[d2].is_empty() || rng.f() < 0.15 {
                s[d2].push(vec![x]);
            } else {
                let t2 = rng.range(s[d2].len());
                let q = rng.range(s[d2][t2].len() + 1);
                s[d2][t2].insert(q, x);
            }
        }
        limpa(&mut s);
        busca_local(inst, &mut s);
        let c = custo(inst, &s);
        if c < best_c - 1e-6 { best = s; best_c = c; }
    }
    best
}

fn otimiza(inst: &Inst, partida: Option<Sol>, seed: u64) -> Sol {
    let ini = partida.unwrap_or_else(|| (0..inst.dias).map(|d| clarke_wright(inst, &pedidos_do_dia(inst, d))).collect());
    ils(inst, ini, ILS_ITERS, seed)
}

// ---------------- Validação ----------------
fn forca_bruta(inst: &Inst, v: &[usize]) -> f64 {
    let mut p = v.to_vec();
    p.sort();
    let mut best = f64::INFINITY;
    loop {
        let a = avalia_viagem(inst, &p);
        if a.ok { best = best.min(a.custo); }
        let mut i = p.len().saturating_sub(1);
        while i > 0 && p[i - 1] >= p[i] { i -= 1; }
        if i == 0 { break; }
        let mut j = p.len() - 1;
        while p[j] <= p[i - 1] { j -= 1; }
        p.swap(i - 1, j);
        p[i..].reverse();
    }
    best
}

fn valida(inst: &Inst, s: &Sol, nome: &str) {
    // todo pedido ativo entregue exatamente uma vez, dentro da janela, frota e jornada respeitadas
    let mut visto = vec![0; inst.ped.len()];
    for (d, dia) in s.iter().enumerate() {
        let mut horas = vec![];
        for v in dia {
            let a = avalia_viagem(inst, v);
            assert!(a.ok, "{nome}: viagem inviável no dia {d}");
            horas.push(a.h);
            for &x in v {
                visto[x] += 1;
                let (j0, j1) = inst.janela[x];
                assert!(j0 <= d && d <= j1, "{nome}: pedido {x} fora da janela");
            }
        }
        assert!(caminhoes_no_dia(&mut horas) <= p().frota, "{nome}: frota estourada no dia {d}");
    }
    for i in 1..inst.ped.len() {
        assert_eq!(visto[i], if inst.ativo[i] { 1 } else { 0 }, "{nome}: pedido {i} entregue {} vezes", visto[i]);
    }
}

// ---------------- Relatório ----------------
struct Res {
    nome: String,
    flex: f64,
    custo: f64,
    km: f64,
    litros: f64,
    h: f64,
    viagens: usize,
    cam_dia: Vec<usize>,
    mesmo_dia: f64,
    espera_media: f64,
}

fn resume(inst: &Inst, s: &Sol, nome: &str, flex: f64) -> Res {
    let mut r = Res { nome: nome.into(), flex, custo: custo_real(inst, s), km: 0.0, litros: 0.0, h: 0.0, viagens: 0,
        cam_dia: vec![], mesmo_dia: 0.0, espera_media: 0.0 };
    let (mut n, mut mesmo, mut espera) = (0.0, 0.0, 0.0);
    for (d, dia) in s.iter().enumerate() {
        let mut horas = vec![];
        for v in dia {
            let a = avalia_viagem(inst, v);
            r.km += a.km; r.litros += a.litros; r.h += a.h; r.viagens += 1;
            horas.push(a.h);
            for &x in v {
                n += 1.0;
                if d == inst.ped[x].dia { mesmo += 1.0; }
                espera += (d - inst.ped[x].dia) as f64;
            }
        }
        r.cam_dia.push(caminhoes_no_dia(&mut horas));
    }
    r.mesmo_dia = 100.0 * mesmo / n;
    r.espera_media = espera / n;
    r
}

fn json_sol(inst: &Inst, s: &Sol, r: &Res) -> String {
    let mut o = String::new();
    let _ = write!(o, r#"{{"nome":"{}","flex":{:.2},"custo":{:.2},"km":{:.1},"litros":{:.1},"horas":{:.1},"viagens":{},"mesmo_dia_pct":{:.1},"caminhoes_dia":{:?},"dias":["#,
        r.nome, r.flex, r.custo, r.km, r.litros, r.h, r.viagens, r.mesmo_dia, r.cam_dia);
    for (d, dia) in s.iter().enumerate() {
        if d > 0 { o.push(','); }
        o.push('[');
        for (t, v) in dia.iter().enumerate() {
            if t > 0 { o.push(','); }
            let a = avalia_viagem(inst, v);
            let _ = write!(o, r#"{{"pedidos":{:?},"km":{:.1},"h":{:.2},"litros":{:.1},"carga":{:.1},"custo":{:.2}}}"#,
                v, a.km, a.h, a.litros, a.carga, custo_real_viagem(&a));
        }
        o.push(']');
    }
    o.push_str("]}");
    o
}

fn main() {
    let raiz = PathBuf::from(std::env::args().nth(1).unwrap_or_else(|| "..".into()));
    PREM.set(ler_premissas(&raiz)).ok();
    let p = p();
    let mut inst = carregar(&raiz);
    let n_ativos = inst.ativo.iter().filter(|&&a| a).count();
    let fora: Vec<&str> = (1..inst.ped.len()).filter(|&i| !inst.ativo[i]).map(|i| inst.ped[i].nome.as_str()).collect();
    println!("{} pedidos em {} dias, frota de {} caminhões ({} t, jornada {} h)", inst.ped.len() - 1, inst.dias, p.frota, p.capacidade_t, p.jornada_h);
    if !fora.is_empty() {
        println!("  {} pedido(s) não cabem numa jornada nem sozinhos (precisam pernoite), fora da comparação: {}", fora.len(), fora.join(", "));
    }
    if p.peso_diesel != 1.0 || p.peso_tempo != 1.0 {
        println!("  busca com pesos: diesel x{}, tempo x{} (os custos mostrados continuam em R$ reais)", p.peso_diesel, p.peso_tempo);
    }
    let t0 = std::time::Instant::now();

    define_janelas(&mut inst, 0.0);
    let a = manual(&inst);
    valida(&inst, &a, "A");
    let ra = resume(&inst, &a, "A) Manual: mais perto, tudo no mesmo dia", 0.0);
    let a2 = manual_regiao(&inst);
    valida(&inst, &a2, "A2");
    let ra2 = resume(&inst, &a2, "A2) Manual por região, tudo no mesmo dia", 0.0);
    let b = otimiza(&inst, None, 1);
    valida(&inst, &b, "B");
    let rb = resume(&inst, &b, "B) Otimizado, tudo no mesmo dia", 0.0);

    let mut curva = vec![];
    let mut sols = vec![];
    for (k, &f) in [0.25, 0.5, 0.75, 1.0].iter().enumerate() {
        define_janelas(&mut inst, f);
        let s = otimiza(&inst, Some(b.clone()), 10 + k as u64);
        valida(&inst, &s, "C");
        let nome = format!("C) Otimizado, {:.0}% aceitam até {} dias", f * 100.0, p.folga_dias);
        curva.push(resume(&inst, &s, &nome, f));
        sols.push(s);
    }
    let tempo = t0.elapsed();

    // validação da ordem das viagens (força bruta) na política B
    define_janelas(&mut inst, 0.0);
    let mut gap: f64 = 0.0;
    let mut checadas = 0;
    for dia in &b {
        for v in dia {
            if v.len() <= 8 {
                gap = gap.max(avalia_viagem(&inst, v).custo / forca_bruta(&inst, v) - 1.0);
                checadas += 1;
            }
        }
    }

    println!("\nOtimização em {:.1?} ({} pedidos ativos)\n", tempo, n_ativos);
    println!("{:<44} {:>7} {:>7} {:>6} {:>6} {:>9} {:>7} {:>8} {:>9} {:>14}", "Política", "km", "litros", "horas", "viag.", "R$", "Δ A", "Δ A2", "mesmo dia", "caminhões/dia");
    let todas: Vec<&Res> = [&ra, &ra2, &rb].into_iter().chain(curva.iter()).collect();
    for r in &todas {
        println!("{:<44} {:>7.0} {:>7.0} {:>6.0} {:>6} {:>9.0} {:>6.1}% {:>7.1}% {:>8.0}% {:>14}",
            r.nome, r.km, r.litros, r.h, r.viagens, r.custo, (r.custo / ra.custo - 1.0) * 100.0, (r.custo / ra2.custo - 1.0) * 100.0, r.mesmo_dia,
            r.cam_dia.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(" "));
    }
    println!("\nValidação: {} viagens de B conferidas por força bruta, maior gap {:.3}%", checadas, gap * 100.0);
    println!("Todas as soluções: cada pedido entregue 1x, dentro da janela, frota e jornada respeitadas.");

    // JSON para o mapa
    let mut js = String::from("{\"politicas\":[");
    define_janelas(&mut inst, 0.0);
    js.push_str(&json_sol(&inst, &a, &ra));
    js.push(',');
    js.push_str(&json_sol(&inst, &b, &rb));
    for (k, s) in sols.iter().enumerate() {
        define_janelas(&mut inst, curva[k].flex);
        js.push(',');
        js.push_str(&json_sol(&inst, s, &curva[k]));
    }
    js.push_str("],\"base_regiao\":");
    define_janelas(&mut inst, 0.0);
    js.push_str(&json_sol(&inst, &a2, &ra2));
    let _ = write!(js, r#","parametros":{{"capacidade_t":{},"jornada_h":{},"frota":{},"folga_dias":{},"diesel_rs_l":{},"motorista_rs_h":{},"caminhao_rs_dia":{},"fator_caminhao":{},"peso_diesel":{},"peso_tempo":{}}},"fora":{:?},"dias":{}}}"#,
        p.capacidade_t, p.jornada_h, p.frota, p.folga_dias, p.diesel_rs_l, p.motorista_rs_h, p.caminhao_rs_dia, p.fator_caminhao, p.peso_diesel, p.peso_tempo,
        (1..inst.ped.len()).filter(|&i| !inst.ativo[i]).collect::<Vec<_>>(), inst.dias);
    fs::create_dir_all(raiz.join("saida")).unwrap();
    fs::write(raiz.join("saida/resultado.json"), js).unwrap();
    println!("\nSoluções salvas em saida/resultado.json");
}
