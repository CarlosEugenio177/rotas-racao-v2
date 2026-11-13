//! Solver multi-dia de roteirização de ração (v2).
//!
//! Entrada (gerada pelos scripts Python):
//!   dados/pedidos.csv   id,nome,lat,lon,demanda_t,dia,u
//!   dados/matriz_km.csv / dados/matriz_h.csv   (OSRM real; ponto 0 = depósito, i = pedido i)
//!
//! Modelo:
//!   - Horizonte de DIAS dias. Cada pedido chega num dia e tem uma janela [dia, dia+folga].
//!     Folga 0 = "tem que sair no mesmo dia".
//!   - Frota de FROTA caminhões/dia. Cada caminhão faz várias viagens no dia, desde que a soma
//!     caiba na jornada (empacotamento First-Fit Decreasing das viagens nos caminhões).
//!   - Custo = diesel (depende da velocidade média do trecho e da carga) + hora motorista
//!     + custo fixo por caminhão usado no dia.
//!
//! Políticas comparadas:
//!   A) Manual: tudo no dia do pedido, rota "vai no mais perto" (o que se faz hoje)
//!   B) Otimizado, tudo no mesmo dia
//!   C) Otimizado com X% dos pedidos aceitando até FOLGA_DIAS de espera (curva 0..100%)

use std::fmt::Write as _;
use std::fs;
use std::path::PathBuf;

// ---------------- Premissas (ajuste com os dados do primo) ----------------
const CAPACIDADE_T: f64 = 12.0;
const JORNADA_H: f64 = 13.0; // por caminhão por dia (sem pernoite)
const DESCARGA_H: f64 = 0.5; // por parada
const CARREGAR_H: f64 = 0.75; // carregar no depósito, por viagem
const FATOR_CAMINHAO: f64 = 1.25; // OSRM perfil carro -> caminhão é ~25% mais lento
const DIESEL_RS_L: f64 = 7.31;
const MOTORISTA_RS_H: f64 = 30.0;
const CAMINHAO_RS_DIA_PADRAO: f64 = 180.0; // depreciação/manutenção por caminhão usado no dia
const CONSUMO_BASE_L_KM: f64 = 0.30; // caminhão vazio em rodovia boa
const ALPHA_CARGA: f64 = 0.35; // +35% consumo totalmente carregado
const FROTA_PADRAO: usize = 5;

/// Custo fixo por caminhão/dia; pode ser sobrescrito pela variável de ambiente CAMINHAO_RS_DIA.
fn caminhao_rs_dia() -> f64 {
    static V: std::sync::OnceLock<f64> = std::sync::OnceLock::new();
    *V.get_or_init(|| std::env::var("CAMINHAO_RS_DIA").ok().and_then(|s| s.parse().ok()).unwrap_or(CAMINHAO_RS_DIA_PADRAO))
}

/// Tamanho da frota; pode ser sobrescrito pela variável de ambiente FROTA.
fn frota() -> usize {
    static V: std::sync::OnceLock<usize> = std::sync::OnceLock::new();
    *V.get_or_init(|| std::env::var("FROTA").ok().and_then(|s| s.parse().ok()).unwrap_or(FROTA_PADRAO))
}
const FOLGA_DIAS: usize = 2;
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
    CONSUMO_BASE_L_KM * (1.0 + 0.8 * ((70.0 - vel_kmh) / 70.0).max(0.0))
}

fn carregar(raiz: &PathBuf) -> Inst {
    let txt = fs::read_to_string(raiz.join("dados/pedidos.csv")).expect("dados/pedidos.csv");
    let mut ped = vec![Pedido { nome: "Depósito".into(), demanda: 0.0, dia: 0, u: 0.0 }];
    for l in txt.lines().skip(1).filter(|l| !l.trim().is_empty()) {
        let c: Vec<&str> = l.split(',').collect();
        ped.push(Pedido {
            nome: c[1].to_string(),
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
            hc[i][j] = h[i][j] * FATOR_CAMINHAO;
        }
    }
    let dias = ped.iter().map(|p| p.dia).max().unwrap() + 1;
    let mut inst = Inst { janela: vec![(0, 0); n], ativo: vec![true; n], ped, litros_vazio: litros, h_cam: hc, km, dias };
    inst.ativo[0] = false;
    for i in 1..n {
        let a = avalia_viagem(&inst, &[i]);
        if a.h > JORNADA_H || inst.ped[i].demanda > CAPACIDADE_T {
            inst.ativo[i] = false;
        }
    }
    inst
}

fn define_janelas(inst: &mut Inst, frac_flex: f64) {
    for i in 1..inst.ped.len() {
        let d = inst.ped[i].dia;
        let folga = if inst.ped[i].u < frac_flex { FOLGA_DIAS } else { 0 };
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
    let carga0: f64 = v.iter().map(|&i| inst.ped[i].demanda).sum();
    let mut a = Av { carga: carga0, h: CARREGAR_H, ..Default::default() };
    let mut carga = carga0;
    let mut prev = 0;
    for &i in v.iter().chain(std::iter::once(&0)) {
        a.km += inst.km[prev][i];
        a.h += inst.h_cam[prev][i];
        a.litros += inst.litros_vazio[prev][i] * (1.0 + ALPHA_CARGA * carga / CAPACIDADE_T);
        if i != 0 {
            a.h += DESCARGA_H;
            carga -= inst.ped[i].demanda;
        }
        prev = i;
    }
    a.custo = a.litros * DIESEL_RS_L + a.h * MOTORISTA_RS_H;
    a.ok = carga0 <= CAPACIDADE_T + 1e-9 && a.h <= JORNADA_H + 1e-9;
    a
}

/// Empacota as viagens do dia nos caminhões (FFD). Devolve nº de caminhões.
fn caminhoes_no_dia(horas: &mut Vec<f64>) -> usize {
    horas.sort_by(|a, b| b.partial_cmp(a).unwrap());
    let mut bins: Vec<f64> = vec![];
    for &h in horas.iter() {
        match bins.iter_mut().find(|b| **b + h <= JORNADA_H + 1e-9) {
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
    c += k as f64 * caminhao_rs_dia();
    if k > frota() { c += PENALIDADE * (k - frota()) as f64; }
    c
}

type Sol = Vec<Vec<Vec<usize>>>; // dia -> viagens -> pedidos

fn custo(inst: &Inst, s: &Sol) -> f64 { s.iter().map(|d| custo_dia(inst, d)).sum() }

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

fn clarke_wright(inst: &Inst, cli: &[usize]) -> Vec<Vec<usize>> {
    let c = |i: usize, j: usize| inst.litros_vazio[i][j] * DIESEL_RS_L + inst.h_cam[i][j] * MOTORISTA_RS_H;
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
    c += k as f64 * caminhao_rs_dia();
    if k > frota() { c += PENALIDADE * (k - frota()) as f64; }
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
        assert!(caminhoes_no_dia(&mut horas) <= frota(), "{nome}: frota estourada no dia {d}");
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
    let mut r = Res { nome: nome.into(), flex, custo: custo(inst, s), km: 0.0, litros: 0.0, h: 0.0, viagens: 0,
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
                v, a.km, a.h, a.litros, a.carga, a.custo);
        }
        o.push(']');
    }
    o.push_str("]}");
    o
}

fn main() {
    let raiz = PathBuf::from(std::env::args().nth(1).unwrap_or_else(|| "..".into()));
    let mut inst = carregar(&raiz);
    let n_ativos = inst.ativo.iter().filter(|&&a| a).count();
    let fora: Vec<&str> = (1..inst.ped.len()).filter(|&i| !inst.ativo[i]).map(|i| inst.ped[i].nome.as_str()).collect();
    println!("{} pedidos em {} dias, frota de {} caminhões ({} t, jornada {} h)", inst.ped.len() - 1, inst.dias, frota(), CAPACIDADE_T, JORNADA_H);
    if !fora.is_empty() {
        println!("  {} pedido(s) não cabem numa jornada nem sozinhos (precisam pernoite), fora da comparação: {}", fora.len(), fora.join(", "));
    }
    let t0 = std::time::Instant::now();

    define_janelas(&mut inst, 0.0);
    let a = manual(&inst);
    valida(&inst, &a, "A");
    let ra = resume(&inst, &a, "A) Manual: mais perto, tudo no mesmo dia", 0.0);
    let b = otimiza(&inst, None, 1);
    valida(&inst, &b, "B");
    let rb = resume(&inst, &b, "B) Otimizado, tudo no mesmo dia", 0.0);

    let mut curva = vec![];
    let mut sols = vec![];
    for (k, &f) in [0.25, 0.5, 0.75, 1.0].iter().enumerate() {
        define_janelas(&mut inst, f);
        let s = otimiza(&inst, Some(b.clone()), 10 + k as u64);
        valida(&inst, &s, "C");
        let nome = format!("C) Otimizado, {:.0}% aceitam até {} dias", f * 100.0, FOLGA_DIAS);
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
    println!("{:<44} {:>7} {:>7} {:>6} {:>6} {:>9} {:>7} {:>9} {:>14}", "Política", "km", "litros", "horas", "viag.", "R$", "Δ", "mesmo dia", "caminhões/dia");
    let todas: Vec<&Res> = [&ra, &rb].into_iter().chain(curva.iter()).collect();
    for r in &todas {
        println!("{:<44} {:>7.0} {:>7.0} {:>6.0} {:>6} {:>9.0} {:>6.1}% {:>8.0}% {:>14}",
            r.nome, r.km, r.litros, r.h, r.viagens, r.custo, (r.custo / ra.custo - 1.0) * 100.0, r.mesmo_dia,
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
    let _ = write!(js, r#"],"parametros":{{"capacidade_t":{CAPACIDADE_T},"jornada_h":{JORNADA_H},"frota":{},"folga_dias":{FOLGA_DIAS},"diesel_rs_l":{DIESEL_RS_L},"motorista_rs_h":{MOTORISTA_RS_H},"caminhao_rs_dia":{},"fator_caminhao":{FATOR_CAMINHAO}}},"fora":{:?},"dias":{}}}"#,
        frota(), caminhao_rs_dia(), (1..inst.ped.len()).filter(|&i| !inst.ativo[i]).collect::<Vec<_>>(), inst.dias);
    fs::create_dir_all(raiz.join("saida")).unwrap();
    fs::write(raiz.join("saida/resultado.json"), js).unwrap();
    println!("\nSoluções salvas em saida/resultado.json");
}
