//! Force-directed 3D layout for the schema universe. Deterministic: same graph in, same positions out
//! (no randomness, and only + - * / sqrt, so the TypeScript fallback produces the same numbers).

use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize)]
pub struct LayoutInput {
    pub nodes: usize,
    #[serde(default)]
    pub edges: Vec<(usize, usize)>,
    #[serde(default = "default_iterations")]
    pub iterations: usize,
    #[serde(default = "default_radius")]
    pub radius: f64,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct LayoutOutput {
    pub positions: Vec<[f64; 3]>,
}

fn default_iterations() -> usize {
    200
}
fn default_radius() -> f64 {
    100.0
}

fn fract(x: f64) -> f64 {
    x - x.floor()
}

/// Low-discrepancy start (R3 sequence): spread out, no randomness.
fn initial(i: usize) -> [f64; 3] {
    let k = (i + 1) as f64;
    [fract(k * 0.8191725133961645) - 0.5, fract(k * 0.6710436067037893) - 0.5, fract(k * 0.5497004779019703) - 0.5]
}

pub fn layout(nodes: usize, edges: &[(usize, usize)], iterations: usize, radius: f64) -> Vec<[f64; 3]> {
    let mut p: Vec<[f64; 3]> = (0..nodes).map(initial).collect();
    if nodes <= 1 {
        return vec![[0.0; 3]; nodes];
    }
    let valid: Vec<(usize, usize)> = edges.iter().copied().filter(|&(a, b)| a != b && a < nodes && b < nodes).collect();
    let mut temp = 0.5;
    for _ in 0..iterations {
        let mut disp = vec![[0.0f64; 3]; nodes];
        for i in 0..nodes {
            for j in (i + 1)..nodes {
                let d = [p[i][0] - p[j][0], p[i][1] - p[j][1], p[i][2] - p[j][2]];
                let dist = (d[0] * d[0] + d[1] * d[1] + d[2] * d[2] + 0.0001).sqrt();
                let f = 1.0 / (dist * dist);
                for a in 0..3 {
                    let v = d[a] / dist * f;
                    disp[i][a] += v;
                    disp[j][a] -= v;
                }
            }
        }
        for &(a, b) in &valid {
            let d = [p[a][0] - p[b][0], p[a][1] - p[b][1], p[a][2] - p[b][2]];
            let dist = (d[0] * d[0] + d[1] * d[1] + d[2] * d[2] + 0.000001).sqrt();
            for k in 0..3 {
                let v = d[k] / dist * dist * dist;
                disp[a][k] -= v;
                disp[b][k] += v;
            }
        }
        for i in 0..nodes {
            for k in 0..3 {
                disp[i][k] -= p[i][k] * 0.05;
            }
            let len = (disp[i][0] * disp[i][0] + disp[i][1] * disp[i][1] + disp[i][2] * disp[i][2]).sqrt();
            if len > 0.0 {
                let step = if len < temp { len } else { temp };
                for k in 0..3 {
                    p[i][k] += disp[i][k] / len * step;
                }
            }
        }
        temp *= 0.97;
    }
    // centre on the centroid, scale so the farthest node sits at `radius`
    let n = nodes as f64;
    let mut c = [0.0f64; 3];
    for q in &p {
        for k in 0..3 {
            c[k] += q[k] / n;
        }
    }
    let mut far = 0.0f64;
    for q in p.iter_mut() {
        for k in 0..3 {
            q[k] -= c[k];
        }
        let r = (q[0] * q[0] + q[1] * q[1] + q[2] * q[2]).sqrt();
        if r > far {
            far = r;
        }
    }
    if far > 0.0 {
        let s = radius / far;
        for q in p.iter_mut() {
            for k in 0..3 {
                q[k] *= s;
            }
        }
    }
    p
}

pub fn layout_json(input: &str) -> String {
    match serde_json::from_str::<LayoutInput>(input) {
        Ok(i) => serde_json::to_string(&LayoutOutput { positions: layout(i.nodes, &i.edges, i.iterations, i.radius) }).unwrap(),
        Err(e) => format!("{{\"error\":{}}}", serde_json::to_string(&e.to_string()).unwrap()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dist(a: [f64; 3], b: [f64; 3]) -> f64 {
        ((a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2) + (a[2] - b[2]).powi(2)).sqrt()
    }

    #[test]
    fn deterministic_and_finite() {
        let e = [(0, 1), (1, 2), (2, 3), (3, 0)];
        let a = layout(4, &e, 200, 100.0);
        let b = layout(4, &e, 200, 100.0);
        assert_eq!(a, b);
        assert!(a.iter().flatten().all(|v| v.is_finite()));
    }

    #[test]
    fn trivial_sizes() {
        assert!(layout(0, &[], 50, 100.0).is_empty());
        assert_eq!(layout(1, &[], 50, 100.0), vec![[0.0; 3]]);
    }

    #[test]
    fn connected_nodes_end_up_closer_than_strangers() {
        // two triangles joined by nothing: within-cluster distance < between-cluster distance
        let e = [(0, 1), (1, 2), (2, 0), (3, 4), (4, 5), (5, 3)];
        let p = layout(6, &e, 300, 100.0);
        let within = (dist(p[0], p[1]) + dist(p[3], p[4])) / 2.0;
        let between = dist(p[0], p[3]);
        assert!(within < between, "within {within} between {between}");
    }

    #[test]
    fn scaled_to_radius_and_centred() {
        let p = layout(5, &[(0, 1), (1, 2)], 100, 50.0);
        let far = p.iter().map(|q| (q[0] * q[0] + q[1] * q[1] + q[2] * q[2]).sqrt()).fold(0.0, f64::max);
        assert!((far - 50.0).abs() < 1e-9);
    }

    #[test]
    fn ignores_bad_edges() {
        let p = layout(2, &[(0, 0), (0, 9)], 20, 10.0);
        assert_eq!(p.len(), 2);
    }

    #[test]
    fn json_roundtrip_and_error() {
        let out = layout_json(r#"{"nodes":2,"edges":[[0,1]]}"#);
        assert!(out.starts_with("{\"positions\":[["));
        assert!(layout_json("nope").contains("error"));
    }

    #[test]
    fn golden_file_matches() {
        let cases: serde_json::Value = serde_json::from_str(include_str!("../testdata/layout_cases.json")).unwrap();
        for c in cases.as_array().unwrap() {
            let inp = serde_json::to_string(&c["input"]).unwrap();
            let out: serde_json::Value = serde_json::from_str(&layout_json(&inp)).unwrap();
            let want = c["expected"]["positions"].as_array().unwrap();
            let got = out["positions"].as_array().unwrap();
            assert_eq!(got.len(), want.len(), "{}", c["name"]);
            for (g, w) in got.iter().zip(want) {
                for k in 0..3 {
                    assert!((g[k].as_f64().unwrap() - w[k].as_f64().unwrap()).abs() < 1e-6, "{}", c["name"]);
                }
            }
        }
    }
}
