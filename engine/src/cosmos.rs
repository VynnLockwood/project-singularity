//! The look of the app: a deterministic starfield and a Keplerian accretion disk around a black hole.
//! Only the parameters are made here (seeded xorshift, plain arithmetic, no trig); the browser draws and
//! animates them. The TypeScript twin in the frontend produces the same numbers from the same seed.

use serde::{Deserialize, Serialize};

const TAU: f64 = 6.283185307179586;

#[derive(Debug, Deserialize)]
pub struct CosmosInput {
    #[serde(default = "d_seed")]
    pub seed: u32,
    #[serde(default = "d_stars")]
    pub stars: usize,
    #[serde(default = "d_particles")]
    pub particles: usize,
}
fn d_seed() -> u32 {
    1
}
fn d_stars() -> usize {
    220
}
fn d_particles() -> usize {
    1600
}

/// A star: position in 0..1 of the canvas, size in px, brightness 0..1, warmth 0 (white) .. 1 (amber).
#[derive(Debug, Serialize, PartialEq, Clone, Copy)]
pub struct Star {
    pub x: f64,
    pub y: f64,
    pub size: f64,
    pub alpha: f64,
    pub warm: f64,
}

/// A disk particle: orbit angle (radians), orbit radius as a fraction 0.16..1 of the disk,
/// angular speed (faster inside, like real orbits), size in px, brightness, hue in degrees (orange to yellow).
#[derive(Debug, Serialize, PartialEq, Clone, Copy)]
pub struct Particle {
    pub angle: f64,
    pub radius: f64,
    pub speed: f64,
    pub size: f64,
    pub alpha: f64,
    pub hue: f64,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct Cosmos {
    pub stars: Vec<Star>,
    pub disk: Vec<Particle>,
}

struct Rng(u32);
impl Rng {
    fn new(seed: u32) -> Self {
        Rng(if seed == 0 { 0x9E37_79B9 } else { seed })
    }
    fn next(&mut self) -> f64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        self.0 = x;
        x as f64 / 4294967296.0
    }
}

pub fn cosmos(seed: u32, stars: usize, particles: usize) -> Cosmos {
    let mut r = Rng::new(seed);
    let stars = (0..stars)
        .map(|_| {
            let (x, y, s, a, w) = (r.next(), r.next(), r.next(), r.next(), r.next());
            Star { x, y, size: 0.4 + s * s * 1.6, alpha: 0.15 + a * 0.85, warm: w }
        })
        .collect();
    let disk = (0..particles)
        .map(|_| {
            let (u, a, s, b, h) = (r.next(), r.next(), r.next(), r.next(), r.next());
            let radius = 0.16 + 0.84 * u * u; // denser near the hole
            Particle {
                angle: a * TAU,
                radius,
                speed: 1.0 / (radius * radius.sqrt()),
                size: 0.5 + s * 1.5,
                alpha: 0.2 + 0.8 * (1.0 - u) * (0.4 + 0.6 * b),
                hue: 18.0 + 30.0 * (1.0 - u) + h * 6.0,
            }
        })
        .collect();
    Cosmos { stars, disk }
}

pub fn cosmos_json(input: &str) -> String {
    match serde_json::from_str::<CosmosInput>(input) {
        Ok(i) => serde_json::to_string(&cosmos(i.seed, i.stars, i.particles)).unwrap(),
        Err(e) => format!("{{\"error\":{}}}", serde_json::to_string(&e.to_string()).unwrap()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_seed_same_sky() {
        assert_eq!(cosmos(7, 50, 100), cosmos(7, 50, 100));
        assert_ne!(cosmos(7, 50, 100), cosmos(8, 50, 100));
    }

    #[test]
    fn counts_and_ranges() {
        let c = cosmos(1, 200, 500);
        assert_eq!((c.stars.len(), c.disk.len()), (200, 500));
        assert!(c.stars.iter().all(|s| (0.0..1.0).contains(&s.x) && (0.0..1.0).contains(&s.y) && s.alpha <= 1.0));
        assert!(c.disk.iter().all(|p| p.radius >= 0.16 && p.radius <= 1.0 && p.alpha <= 1.0 && p.angle < TAU));
    }

    #[test]
    fn inner_orbits_are_faster() {
        let c = cosmos(3, 0, 400);
        let inner = c.disk.iter().filter(|p| p.radius < 0.3).map(|p| p.speed).fold(f64::MAX, f64::min);
        let outer = c.disk.iter().filter(|p| p.radius > 0.8).map(|p| p.speed).fold(0.0, f64::max);
        assert!(inner > outer);
    }

    #[test]
    fn zero_seed_still_works_and_bad_json_errors() {
        assert_eq!(cosmos(0, 3, 3).stars.len(), 3);
        assert!(cosmos_json("nope").contains("error"));
        assert!(cosmos_json("{}").starts_with("{\"stars\":["));
    }

    #[test]
    fn golden_file_matches() {
        let cases: serde_json::Value = serde_json::from_str(include_str!("../testdata/cosmos_cases.json")).unwrap();
        for c in cases.as_array().unwrap() {
            let out: serde_json::Value = serde_json::from_str(&cosmos_json(&serde_json::to_string(&c["input"]).unwrap())).unwrap();
            assert_eq!(out, c["expected"], "{}", c["name"]);
        }
    }
}
