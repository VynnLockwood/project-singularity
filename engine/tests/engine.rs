use serde_json::Value;
use singularity_engine::{column_stats_json, find_duplicates, normalize, Record};

fn rec(id: i64, name: &str) -> Record {
    Record { id, fields: vec![name.to_string()] }
}
fn groups(names: &[&str], threshold: f64) -> Vec<Vec<i64>> {
    let recs: Vec<Record> = names.iter().enumerate().map(|(i, n)| rec(i as i64 + 1, n)).collect();
    find_duplicates(&recs, threshold).into_iter().map(|g| g.ids).collect()
}

// ---- normalize -----------------------------------------------------------------

#[test]
fn normalize_handles_case_punctuation_and_company_forms() {
    assert_eq!(normalize("  ACME   Trading, Co., Ltd. "), "acme trading");
    assert_eq!(normalize("Acme Trading Company Limited"), "acme trading");
    assert_eq!(normalize("The Acme Corp."), "acme");
    assert_eq!(normalize("Acme-Trading_Inc"), "acme trading");
}

#[test]
fn normalize_keeps_a_name_that_is_only_a_company_form() {
    assert_eq!(normalize("Limited"), "limited");
    assert_eq!(normalize("Co., Ltd."), "co ltd");
}

#[test]
fn normalize_handles_thai() {
    assert_eq!(normalize("บริษัท สยามพัฒนา จำกัด"), "สยามพัฒนา");
    assert_eq!(normalize("บริษัท สยามพัฒนา จำกัด (มหาชน)"), "สยามพัฒนา");
    assert_eq!(normalize("บจก.สยามพัฒนา"), "สยามพัฒนา");
    assert_eq!(normalize("สยาม\u{200B}พัฒนา"), "สยามพัฒนา"); // zero-width space
    assert_eq!(normalize("ร้าน ๑๒๓"), "ร้าน 123"); // Thai digits
    assert_eq!(normalize("สยามพัฒนา\u{0E48}"), "สยามพัฒนา\u{0E48}"); // tone marks survive
}

#[test]
fn normalize_unifies_width_and_is_idempotent() {
    assert_eq!(normalize("ＡＣＭＥ"), "acme");
    for s in ["Acme Co., Ltd.", "บริษัท ก จำกัด", "  x  ", "", "..."] {
        assert_eq!(normalize(&normalize(s)), normalize(s), "{s:?}");
    }
    assert_eq!(normalize(""), "");
    assert_eq!(normalize("   ...  "), "");
}

// ---- dedup ---------------------------------------------------------------------

#[test]
fn finds_typos_suffix_variants_and_reordered_names() {
    let g = groups(
        &["Acme Trading Co., Ltd.", "ACME TRADING COMPANY LIMITED", "Acme Tradng Co Ltd", "Trading Acme", "Zenith Foods"],
        0.9,
    );
    assert_eq!(g, vec![vec![1, 2, 3, 4]]);
}

#[test]
fn finds_thai_variants() {
    let g = groups(&["บริษัท สยามพัฒนา จำกัด", "สยามพัฒนา", "บจก. สยามพัฒนา", "บริษัท ไทยเจริญ จำกัด"], 0.9);
    assert_eq!(g, vec![vec![1, 2, 3]]);
}

#[test]
fn thai_spelling_slips_are_caught() {
    let g = groups(&["สยามพัฒนาการ", "สยามพัฒนากาน", "ไทยเจริญ"], 0.9);
    assert_eq!(g, vec![vec![1, 2]]);
}

#[test]
fn different_things_stay_apart() {
    assert!(groups(&["Acme Trading", "Beta Logistics", "Gamma Foods", "Delta Steel"], 0.9).is_empty());
    assert!(groups(&["Smith", "Smyth Brothers Plumbing"], 0.9).is_empty());
}

#[test]
fn short_strings_are_not_overmatched() {
    assert!(groups(&["ab", "abc", "abd"], 0.95).is_empty());
}

#[test]
fn threshold_controls_strictness() {
    let names = ["Acme Trading", "Acme Trader"];
    assert_eq!(groups(&names, 0.9).len(), 1);
    assert!(groups(&names, 0.999).is_empty());
}

#[test]
fn an_exact_name_scores_one_and_the_group_reports_its_weakest_link() {
    let recs = vec![rec(1, "Acme Trading"), rec(2, "acme  trading"), rec(3, "Acme Tradng")];
    let g = find_duplicates(&recs, 0.9);
    assert_eq!(g.len(), 1);
    assert_eq!(g[0].ids, vec![1, 2, 3]);
    assert!(g[0].score < 1.0 && g[0].score >= 0.9);
    let pair = find_duplicates(&recs[..2], 0.9);
    assert_eq!(pair[0].score, 1.0);
}

#[test]
fn empty_fields_never_match_each_other() {
    assert!(groups(&["", "", "  ", "..."], 0.5).is_empty());
}

#[test]
fn compares_field_by_field_and_takes_the_best_pair() {
    let recs = vec![
        Record { id: 1, fields: vec!["Acme Trading".into(), "".into()] },
        Record { id: 2, fields: vec!["Totally Different".into(), "".into()] },
        Record { id: 3, fields: vec!["Other Name".into(), "".into()] },
    ];
    assert!(find_duplicates(&recs, 0.9).is_empty()); // empty trade names do not link 1, 2 and 3
    let recs = vec![
        Record { id: 1, fields: vec!["Acme Trading".into(), "ACME".into()] },
        Record { id: 2, fields: vec!["Something Else".into(), "Acme".into()] },
    ];
    assert_eq!(find_duplicates(&recs, 0.9).len(), 1); // the second field matches
}

#[test]
fn output_is_deterministic_and_sorted() {
    let names = ["Beta Co", "Acme Ltd", "acme", "beta", "Acme Inc"];
    let first = groups(&names, 0.9);
    for _ in 0..20 {
        assert_eq!(groups(&names, 0.9), first);
    }
    assert_eq!(first, vec![vec![1, 4], vec![2, 3, 5]]);
}

#[test]
fn a_huge_common_block_does_not_blow_up_or_link_everything() {
    // 2000 different names that all share the word "trading": the shared word is not evidence.
    let word = |i: usize| -> String {
        let mut n = i * 7919 + 12345;
        (0..6).map(|_| { let c = (b'a' + (n % 26) as u8) as char; n /= 26; n += 7; c }).collect()
    };
    let owned: Vec<String> = (0..2000).map(|i| format!("{} trading", word(i))).collect();
    let names: Vec<&str> = owned.iter().map(|s| s.as_str()).collect();
    let started = std::time::Instant::now();
    let g = groups(&names, 0.95);
    assert!(g.iter().all(|x| x.len() < 50), "unrelated names were chained together");
    assert!(started.elapsed().as_secs() < 20, "blocking should keep this fast");
}

#[test]
fn duplicate_ids_do_not_break_grouping() {
    let recs = vec![rec(1, "Acme"), rec(1, "Acme"), rec(2, "Acme")];
    let g = find_duplicates(&recs, 0.9);
    assert_eq!(g[0].ids, vec![1, 2]);
}

// ---- stats (shared golden cases, also run against the TypeScript fallback) --------

#[test]
fn stats_match_the_golden_cases() {
    let cases: Vec<Value> = serde_json::from_str(include_str!("../testdata/stats_cases.json")).unwrap();
    for c in cases {
        let input = serde_json::to_string(&c["input"]).unwrap();
        let got: Value = serde_json::from_str(&column_stats_json(&input)).unwrap();
        assert_eq!(got, c["expected"], "case {}", c["name"]);
    }
}

#[test]
fn stats_reject_non_arrays_with_an_error_object() {
    let got: Value = serde_json::from_str(&column_stats_json("{\"a\":1}")).unwrap();
    assert!(got["error"].as_str().unwrap().contains("array"));
    let got: Value = serde_json::from_str(&column_stats_json("not json")).unwrap();
    assert!(got["error"].is_string());
}
