//! Duplicate detection: normalise -> block -> score -> group.
//!
//! Comparing every pair is O(n^2), so records are only compared when they share a *block key*
//! (a prefix, a suffix, or a leading word of a normalised field). A key shared by more than
//! `MAX_BLOCK` records says little about identity and is skipped. Scores combine Jaro-Winkler
//! (typos), a word-order-insensitive variant (reordered names) and character-bigram overlap
//! (Thai, which has no word breaks). Pairs at or above the threshold are merged into groups.

use std::collections::{HashMap, HashSet};

use crate::normalize::{normalize, tokens};

pub const MAX_BLOCK: usize = 300;

#[derive(Debug, Clone)]
pub struct Record {
    pub id: i64,
    /// The fields to compare (for example name, trade name). Compared field against same-index field.
    pub fields: Vec<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Group {
    pub ids: Vec<i64>,
    /// The weakest link between members, 0..1.
    pub score: f64,
}

fn sorted_tokens(n: &str) -> String {
    let mut t = tokens(n);
    t.sort_unstable();
    t.join(" ")
}

fn bigrams(s: &str) -> HashMap<(char, char), usize> {
    let cs: Vec<char> = s.chars().filter(|c| *c != ' ').collect();
    let mut m = HashMap::new();
    for w in cs.windows(2) {
        *m.entry((w[0], w[1])).or_insert(0) += 1;
    }
    m
}

fn dice(a: &str, b: &str) -> f64 {
    let (x, y) = (bigrams(a), bigrams(b));
    let total: usize = x.values().sum::<usize>() + y.values().sum::<usize>();
    if total == 0 {
        return 0.0;
    }
    let shared: usize = x.iter().map(|(k, v)| (*v).min(*y.get(k).unwrap_or(&0))).sum();
    2.0 * shared as f64 / total as f64
}

/// Similarity of two *normalised* strings, 0..1. Empty never matches anything.
pub fn similarity(a: &str, b: &str) -> f64 {
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    if a == b {
        return 1.0;
    }
    let jw = strsim::jaro_winkler(a, b);
    let reordered = strsim::jaro_winkler(&sorted_tokens(a), &sorted_tokens(b));
    // Bigram overlap suits Thai (no word breaks) and long names; on short strings it is too forgiving.
    let long_enough = a.chars().filter(|c| *c != ' ').count() >= 5 && b.chars().filter(|c| *c != ' ').count() >= 5;
    let overlap = if long_enough { dice(a, b) } else { 0.0 };
    jw.max(reordered).max(overlap)
}

fn block_keys(n: &str) -> Vec<String> {
    let mut keys = Vec::new();
    let chars: Vec<char> = n.chars().collect();
    if chars.len() >= 3 {
        keys.push(format!("p:{}", chars[..3].iter().collect::<String>()));
        keys.push(format!("s:{}", chars[chars.len() - 3..].iter().collect::<String>()));
        let st: Vec<char> = sorted_tokens(n).chars().collect();
        if st.len() >= 3 {
            keys.push(format!("o:{}", st[..3].iter().collect::<String>()));
        }
    } else if !chars.is_empty() {
        keys.push(format!("e:{n}"));
    }
    for t in tokens(n) {
        let tc: Vec<char> = t.chars().collect();
        if tc.len() >= 4 {
            keys.push(format!("w:{}", tc[..4].iter().collect::<String>()));
        }
    }
    keys
}

struct Dsu {
    parent: Vec<usize>,
}
impl Dsu {
    fn new(n: usize) -> Self {
        Dsu { parent: (0..n).collect() }
    }
    fn find(&mut self, x: usize) -> usize {
        let mut r = x;
        while self.parent[r] != r {
            r = self.parent[r];
        }
        let mut c = x;
        while self.parent[c] != r {
            let next = self.parent[c];
            self.parent[c] = r;
            c = next;
        }
        r
    }
    fn union(&mut self, a: usize, b: usize) {
        let (ra, rb) = (self.find(a), self.find(b));
        if ra != rb {
            self.parent[ra.max(rb)] = ra.min(rb);
        }
    }
}

/// Groups of records that look like the same thing. `threshold` is 0..1 (0.9 is a good start).
/// Records are compared field by field (same index); a pair scores as its best-matching field.
/// Output is deterministic: members sorted by id, groups by first id.
pub fn find_duplicates(records: &[Record], threshold: f64) -> Vec<Group> {
    let threshold = threshold.clamp(0.0, 1.0);
    let norm: Vec<Vec<String>> = records.iter().map(|r| r.fields.iter().map(|f| normalize(f)).collect()).collect();

    // block key -> record indexes
    let mut blocks: HashMap<(usize, String), Vec<usize>> = HashMap::new();
    for (i, fields) in norm.iter().enumerate() {
        for (fi, f) in fields.iter().enumerate() {
            for k in block_keys(f) {
                blocks.entry((fi, k)).or_default().push(i);
            }
        }
    }

    let mut seen: HashSet<(usize, usize)> = HashSet::new();
    let mut dsu = Dsu::new(records.len());
    let mut edges: Vec<(usize, usize, f64)> = Vec::new();
    for members in blocks.values() {
        if members.len() < 2 || members.len() > MAX_BLOCK {
            continue;
        }
        for (x, &a) in members.iter().enumerate() {
            for &b in &members[x + 1..] {
                let pair = (a.min(b), a.max(b));
                if !seen.insert(pair) {
                    continue;
                }
                let score = norm[pair.0]
                    .iter()
                    .zip(&norm[pair.1])
                    .map(|(fa, fb)| similarity(fa, fb))
                    .fold(0.0, f64::max);
                if score >= threshold {
                    dsu.union(pair.0, pair.1);
                    edges.push((pair.0, pair.1, score));
                }
            }
        }
    }

    let mut weakest: HashMap<usize, f64> = HashMap::new();
    for (a, _, s) in &edges {
        let root = dsu.find(*a);
        let e = weakest.entry(root).or_insert(1.0);
        *e = e.min(*s);
    }
    let mut by_root: HashMap<usize, Vec<i64>> = HashMap::new();
    for (i, record) in records.iter().enumerate() {
        let r = dsu.find(i);
        if weakest.contains_key(&r) {
            by_root.entry(r).or_default().push(record.id);
        }
    }
    let mut groups: Vec<Group> = by_root
        .into_iter()
        .map(|(root, mut ids)| {
            ids.sort_unstable();
            ids.dedup();
            Group { ids, score: weakest[&root] }
        })
        .filter(|g| g.ids.len() > 1)
        .collect();
    groups.sort_by_key(|g| g.ids[0]);
    groups
}
