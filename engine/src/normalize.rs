//! Text normalisation for comparing names: case, width, whitespace, punctuation, company suffixes, Thai quirks.

use unicode_normalization::UnicodeNormalization;

/// Company-form words that say nothing about *which* company it is. Removed as whole tokens.
const SUFFIX_TOKENS: &[&str] = &[
    "co", "company", "corp", "corporation", "inc", "incorporated", "ltd", "limited", "llc", "plc", "pcl", "public",
    "pte", "gmbh", "sa", "bv", "the",
];

/// Thai company markers, removed wherever they appear as words or as a prefix/suffix of the whole name.
const THAI_MARKERS: &[&str] = &["บริษัท", "บจก", "บมจ", "หจก", "ห้างหุ้นส่วนจำกัด", "จำกัด(มหาชน)", "(มหาชน)", "มหาชน", "จำกัด"];

fn is_ignorable(c: char) -> bool {
    // zero-width space/joiners, BOM, soft hyphen
    matches!(c, '\u{200B}' | '\u{200C}' | '\u{200D}' | '\u{2060}' | '\u{FEFF}' | '\u{00AD}')
}

fn thai_digit(c: char) -> char {
    match c {
        '๐'..='๙' => char::from_digit(c as u32 - '๐' as u32, 10).unwrap(),
        _ => c,
    }
}

/// Lower-case, NFKC, Thai digits to ASCII, drop zero-width characters, punctuation to spaces,
/// collapse whitespace, then drop company-form words ("Co., Ltd.", "บริษัท … จำกัด").
/// If that would leave nothing (the name *is* "Limited"), the cleaned text is kept.
pub fn normalize(input: &str) -> String {
    let cleaned: String = input
        .nfkc()
        .filter(|c| !is_ignorable(*c))
        .map(thai_digit)
        .flat_map(|c| c.to_lowercase())
        .map(|c| if c.is_alphanumeric() || is_thai_mark(c) { c } else { ' ' })
        .collect();

    let mut text = cleaned.split_whitespace().collect::<Vec<_>>().join(" ");

    // Thai has no spaces between words, so markers are removed as substrings of the whole text.
    let mut stripped = text.clone();
    for m in THAI_MARKERS {
        // Same NFKC treatment as the text (it turns sara am into nikhahit + sara aa), or they would never match.
        let m: String = m.nfkc().filter(|c| c.is_alphanumeric() || is_thai_mark(*c)).collect();
        if !m.is_empty() {
            stripped = stripped.replace(&m, " ");
        }
    }
    let stripped = stripped.split_whitespace().collect::<Vec<_>>().join(" ");

    let kept: Vec<&str> = stripped.split(' ').filter(|t| !t.is_empty() && !SUFFIX_TOKENS.contains(t)).collect();
    if kept.is_empty() {
        if !stripped.is_empty() {
            text = stripped;
        }
        return text;
    }
    kept.join(" ")
}

/// Thai vowel signs and tone marks are combining marks, not alphanumeric; keep them.
fn is_thai_mark(c: char) -> bool {
    ('\u{0E31}'..='\u{0E3A}').contains(&c) || ('\u{0E47}'..='\u{0E4E}').contains(&c)
}

pub fn tokens(normalized: &str) -> Vec<&str> {
    normalized.split(' ').filter(|t| !t.is_empty()).collect()
}
