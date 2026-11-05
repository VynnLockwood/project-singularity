//! Singularity engine: pure Rust core, with thin bindings for Python (feature `python`) and the browser (feature `wasm`).

pub mod dedup;
pub mod layout;
pub mod normalize;
pub mod stats;

pub use dedup::{find_duplicates, Group, Record};
pub use normalize::normalize;
pub use layout::layout_json;
pub use stats::column_stats_json;

#[cfg(feature = "python")]
mod python {
    use pyo3::prelude::*;

    use crate::dedup::{find_duplicates, Record};

    /// find_duplicates(records, threshold=0.9) -> [(ids, score)]
    /// records: [(id, [field, ...]), ...]
    #[pyfunction]
    #[pyo3(name = "find_duplicates", signature = (records, threshold=0.9))]
    fn find_duplicates_py(py: Python<'_>, records: Vec<(i64, Vec<String>)>, threshold: f64) -> Vec<(Vec<i64>, f64)> {
        let recs: Vec<Record> = records.into_iter().map(|(id, fields)| Record { id, fields }).collect();
        let groups = py.allow_threads(|| find_duplicates(&recs, threshold));
        groups.into_iter().map(|g| (g.ids, g.score)).collect()
    }

    #[pyfunction]
    #[pyo3(name = "normalize")]
    fn normalize_py(text: &str) -> String {
        crate::normalize::normalize(text)
    }

    #[pyfunction]
    #[pyo3(name = "column_stats")]
    fn column_stats_py(values_json: &str) -> String {
        crate::stats::column_stats_json(values_json)
    }

    #[pyfunction]
    #[pyo3(name = "layout")]
    fn layout_py(input_json: &str) -> String {
        crate::layout::layout_json(input_json)
    }

    #[pymodule]
    fn singularity_engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(find_duplicates_py, m)?)?;
        m.add_function(wrap_pyfunction!(normalize_py, m)?)?;
        m.add_function(wrap_pyfunction!(column_stats_py, m)?)?;
        m.add_function(wrap_pyfunction!(layout_py, m)?)?;
        Ok(())
    }
}

#[cfg(feature = "wasm")]
mod wasm {
    use wasm_bindgen::prelude::*;

    /// Column statistics for the browser. `values_json` is a JSON array of scalars.
    #[wasm_bindgen(js_name = columnStats)]
    pub fn column_stats(values_json: &str) -> String {
        crate::stats::column_stats_json(values_json)
    }

    /// 3D force layout for the schema universe. JSON in, JSON out (see layout.rs).
    #[wasm_bindgen(js_name = layout3d)]
    pub fn layout3d(input_json: &str) -> String {
        crate::layout::layout_json(input_json)
    }
}
