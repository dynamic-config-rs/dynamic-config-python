//! The resolved tree, across the boundary.
//!
//! Both directions build the target structure directly: a JSON *string*
//! round trip would parse twice and, worse, lose the integer/float
//! distinction that every configuration format keeps — `port = 5432` must
//! not arrive as `5432.0`, and a `u64` above `i64::MAX` must not silently
//! become a float.

use pyo3::prelude::*;
use pyo3::types::IntoPyDict;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use serde_json::{Map, Number, Value};

/// The key figment's TOML provider hides a native date, time or datetime
/// under. Not ours, and not a stable API — but the alternative is handing
/// a schema a one-key dict and calling it a date.
const TOML_DATETIME: &str = "$__toml_private_datetime";

/// The resolved tree as Python data: dicts, lists and scalars.
pub(crate) fn to_py<'py>(py: Python<'py>, value: &Value) -> PyResult<Bound<'py, PyAny>> {
    Ok(match value {
        Value::Null => py.None().into_bound(py),
        Value::Bool(boolean) => boolean.into_pyobject(py)?.to_owned().into_any(),
        Value::Number(number) => number_to_py(py, number)?,
        Value::String(string) => string.into_pyobject(py)?.into_any(),
        Value::Array(items) => {
            let list = PyList::empty(py);
            for item in items {
                list.append(to_py(py, item)?)?;
            }
            list.into_any()
        }
        Value::Object(entries) => {
            // A TOML date is not a table, whatever the tree says it is.
            // figment's TOML provider carries a native date, time or
            // datetime as a one-key map under a private marker, which
            // serde reconstitutes on the Rust side and nothing
            // reconstitutes here — so a `date` field met a dict and every
            // schema refused it, Pydantic's included. Handed over as the
            // text it was written as, which is what a schema can parse.
            if entries.len() == 1 {
                if let Some(Value::String(text)) = entries.get(TOML_DATETIME) {
                    return Ok(text.into_pyobject(py)?.into_any());
                }
            }

            let dict = PyDict::new(py);
            for (key, item) in entries {
                dict.set_item(key, to_py(py, item)?)?;
            }
            dict.into_any()
        }
    })
}

fn number_to_py<'py>(py: Python<'py>, number: &Number) -> PyResult<Bound<'py, PyAny>> {
    if let Some(signed) = number.as_i64() {
        return Ok(signed.into_pyobject(py)?.into_any());
    }
    if let Some(unsigned) = number.as_u64() {
        return Ok(unsigned.into_pyobject(py)?.into_any());
    }

    // Everything a JSON number can be that is not an integer is a float;
    // `as_f64` cannot fail for such a number.
    let float = number.as_f64().unwrap_or(f64::NAN);

    Ok(float.into_pyobject(py)?.into_any())
}

/// Python data as a value the loader's layers accept.
///
/// For the runtime layers (`set_default`, `set_override`) and `replace`,
/// where the value starts on the Python side. Anything without a
/// configuration meaning — a function, an open file — is refused here
/// rather than serialized into something surprising.
pub(crate) fn from_py(object: &Bound<'_, PyAny>) -> PyResult<Value> {
    let py = object.py();

    if object.is_none() {
        return Ok(Value::Null);
    }
    // Before the integer check: `bool` is a subclass of `int` in Python,
    // and `True` arriving as `1` would be a quiet type change.
    if let Ok(boolean) = object.cast::<PyBool>() {
        return Ok(Value::Bool(boolean.is_true()));
    }
    if object.cast::<PyInt>().is_ok() {
        if let Ok(signed) = object.extract::<i64>() {
            return Ok(Value::Number(signed.into()));
        }
        if let Ok(unsigned) = object.extract::<u64>() {
            return Ok(Value::Number(unsigned.into()));
        }

        return Err(pyo3::exceptions::PyOverflowError::new_err(
            "this integer is too large for a configuration value",
        ));
    }
    if object.cast::<PyFloat>().is_ok() {
        let float = object.extract::<f64>()?;

        return Number::from_f64(float).map(Value::Number).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err("NaN and infinity are not configuration values")
        });
    }
    if let Ok(string) = object.cast::<PyString>() {
        return Ok(Value::String(string.extract()?));
    }
    if let Ok(dict) = object.cast::<PyDict>() {
        let mut entries = Map::with_capacity(dict.len());

        for (key, item) in dict.iter() {
            let key: String = key.extract().map_err(|_| {
                pyo3::exceptions::PyTypeError::new_err("configuration keys have to be strings")
            })?;
            entries.insert(key, from_py(&item)?);
        }

        return Ok(Value::Object(entries));
    }
    if object.cast::<PyList>().is_ok() || object.cast::<PyTuple>().is_ok() {
        let mut items = Vec::new();

        for item in object.try_iter()? {
            items.push(from_py(&item?)?);
        }

        return Ok(Value::Array(items));
    }

    // Pydantic's `SecretStr` and `SecretBytes` hide their value from
    // every renderer, which is their whole job — and exactly wrong here,
    // where the caller is *supplying* configuration or asking which paths
    // changed. Comparing the mask instead would make two different
    // passwords look equal, so the value is taken; what leaves this
    // library is still only paths (`changed_paths`) or the value the
    // caller just handed in (`set_default`).
    if let Ok(secret) = object.call_method0(pyo3::intern!(py, "get_secret_value")) {
        return from_py(&secret);
    }

    // An enum is its value — which is what a file writes and what a
    // schema takes back. Neither `model_dump()` nor `dataclasses.asdict`
    // unwraps one, so without this a model holding an `Enum` could not be
    // compared or supplied as a default at all.
    if let Some(value) = enum_value(object)? {
        return from_py(&value);
    }

    // The stdlib types a configuration legitimately holds, each with
    // exactly one text form that parses back: `date`, `time`, `datetime`,
    // `Path`, `Decimal`, `UUID`, the `ipaddress` family. Rendered as that
    // text rather than refused — the alternative is a `changed_paths`
    // that cannot diff a model with a date in it.
    if let Some(text) = stdlib_text(object)? {
        return Ok(Value::String(text));
    }

    // A Pydantic model renders itself as configuration data through the
    // standard door — `by_alias`, because every path in this library is
    // the name a *file* uses. A zero-argument dump spells fields by their
    // Python names, and a model whose field carries `alias="VALUE"` then
    // round-trips to a key `model_validate` does not accept: the default
    // was silently dropped rather than refused. It also lines the paths
    // `changed_paths` reports up with the ones `explain` and the secret
    // list already use.
    if let Ok(dumped) = object.call_method(
        pyo3::intern!(py, "model_dump"),
        (),
        Some(&[(pyo3::intern!(py, "by_alias"), true)].into_py_dict(py)?),
    ) {
        return from_py(&dumped);
    }

    // A plain dataclass has no such method, and the stdlib is the whole of
    // the difference: `asdict` recurses through nested dataclasses, lists
    // and dicts exactly as this conversion wants. The class object itself
    // is not an instance, so it is not configuration.
    if object
        .hasattr(pyo3::intern!(py, "__dataclass_fields__"))
        .unwrap_or(false)
        && !object.is_instance_of::<pyo3::types::PyType>()
    {
        let dataclasses = py.import(pyo3::intern!(py, "dataclasses"))?;
        let dumped = dataclasses.call_method1(pyo3::intern!(py, "asdict"), (object,))?;

        return from_py(&dumped);
    }

    Err(pyo3::exceptions::PyTypeError::new_err(format!(
        "{} is not a configuration value: expected a string, number, bool, \
         None, list, dict, a Pydantic model or a dataclass",
        object
            .get_type()
            .name()
            .map(|name| name.to_string())
            .unwrap_or_else(|_| "this object".to_owned())
    )))
}

/// An enum member's value, or `None` for anything that is not one.
fn enum_value<'py>(object: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = object.py();
    let enum_module = py.import(pyo3::intern!(py, "enum"))?;
    let base = enum_module.getattr(pyo3::intern!(py, "Enum"))?;

    if object.is_instance(&base)? {
        return Ok(Some(object.getattr(pyo3::intern!(py, "value"))?));
    }

    Ok(None)
}

/// The one canonical text form of a stdlib type a configuration holds.
///
/// Each of these parses back from exactly what it prints, which is what
/// makes the round trip safe: `Decimal("1.5")`, `UUID(...)`,
/// `date.fromisoformat(...)`, `Path(...)`. A type without that property
/// is not handled here — it falls through to the refusal, which is the
/// honest answer for something a configuration file cannot express.
fn stdlib_text(object: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    let py = object.py();

    // `date`, `time`, `datetime` — and anything else that spells itself
    // the way ISO 8601 does.
    if let Ok(rendered) = object.call_method0(pyo3::intern!(py, "isoformat")) {
        return Ok(Some(rendered.extract()?));
    }

    // `Path`, and anything else that is a filesystem path.
    if object
        .hasattr(pyo3::intern!(py, "__fspath__"))
        .unwrap_or(false)
    {
        let os = py.import(pyo3::intern!(py, "os"))?;
        let rendered = os.call_method1(pyo3::intern!(py, "fspath"), (object,))?;

        return Ok(Some(rendered.extract()?));
    }

    let module = object
        .get_type()
        .getattr(pyo3::intern!(py, "__module__"))
        .and_then(|name| name.extract::<String>())
        .unwrap_or_default();

    if matches!(module.as_str(), "decimal" | "uuid" | "ipaddress") {
        return Ok(Some(object.str()?.extract()?));
    }

    Ok(None)
}
