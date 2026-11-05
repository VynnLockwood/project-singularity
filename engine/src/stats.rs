//! Column statistics on a sample of values. Input and output are JSON so the same code serves
//! Python, the browser (WASM) and tests, and the TypeScript fallback can be checked against the same cases.

use std::collections::HashMap;

use serde::Serialize;
use serde_json::Value;

const TOP: usize = 5;

#[derive(Debug, Serialize, PartialEq)]
pub struct Top {
    pub value: String,
    pub count: usize,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct Numeric {
    pub min: f64,
    pub max: f64,
    pub mean: f64,
    pub median: f64,
    pub stddev: f64,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct Text {
    #[serde(rename = "minLength")]
    pub min_length: usize,
    #[serde(rename = "maxLength")]
    pub max_length: usize,
    #[serde(rename = "avgLength")]
    pub avg_length: f64,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct Stats {
    pub count: usize,
    pub nulls: usize,
    #[serde(rename = "nullPct")]
    pub null_pct: f64,
    pub distinct: usize,
    pub top: Vec<Top>,
    pub numeric: Option<Numeric>,
    pub text: Option<Text>,
}

fn show(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

fn round(x: f64, places: i32) -> f64 {
    let k = 10f64.powi(places);
    (x * k).round() / k
}

/// `values` is any JSON array of scalars; null and "" count as missing... only null does. Empty strings are values.
pub fn column_stats(values: &[Value]) -> Stats {
    let count = values.len();
    let present: Vec<&Value> = values.iter().filter(|v| !v.is_null()).collect();
    let nulls = count - present.len();

    let mut freq: HashMap<String, usize> = HashMap::new();
    for v in &present {
        *freq.entry(show(v)).or_insert(0) += 1;
    }
    let distinct = freq.len();
    let mut top: Vec<Top> = freq.into_iter().map(|(value, count)| Top { value, count }).collect();
    top.sort_by(|a, b| b.count.cmp(&a.count).then_with(|| a.value.cmp(&b.value)));
    top.truncate(TOP);

    let numbers: Vec<f64> = present
        .iter()
        .filter_map(|v| match v {
            Value::Number(n) => n.as_f64(),
            Value::String(s) => s.trim().parse::<f64>().ok().filter(|x| x.is_finite() && !s.trim().is_empty()),
            _ => None,
        })
        .collect();
    let numeric = if !present.is_empty() && numbers.len() == present.len() {
        let mut sorted = numbers.clone();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let n = sorted.len() as f64;
        let mean = sorted.iter().sum::<f64>() / n;
        let median = if sorted.len() % 2 == 1 {
            sorted[sorted.len() / 2]
        } else {
            (sorted[sorted.len() / 2 - 1] + sorted[sorted.len() / 2]) / 2.0
        };
        let var = sorted.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / n;
        Some(Numeric {
            min: sorted[0],
            max: sorted[sorted.len() - 1],
            mean: round(mean, 6),
            median: round(median, 6),
            stddev: round(var.sqrt(), 6),
        })
    } else {
        None
    };

    let text = if numeric.is_none() && !present.is_empty() && present.iter().all(|v| v.is_string()) {
        let lens: Vec<usize> = present.iter().map(|v| v.as_str().unwrap().chars().count()).collect();
        Some(Text {
            min_length: *lens.iter().min().unwrap(),
            max_length: *lens.iter().max().unwrap(),
            avg_length: round(lens.iter().sum::<usize>() as f64 / lens.len() as f64, 3),
        })
    } else {
        None
    };

    Stats {
        count,
        nulls,
        null_pct: if count == 0 { 0.0 } else { round(nulls as f64 * 100.0 / count as f64, 3) },
        distinct,
        top,
        numeric,
        text,
    }
}

/// JSON in, JSON out. Errors come back as `{"error": "..."}`.
pub fn column_stats_json(input: &str) -> String {
    match serde_json::from_str::<Vec<Value>>(input) {
        Ok(values) => serde_json::to_string(&column_stats(&values)).unwrap(),
        Err(e) => serde_json::json!({ "error": format!("expected a JSON array: {e}") }).to_string(),
    }
}
