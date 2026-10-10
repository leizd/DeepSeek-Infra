//! `python_eval` — the oracle's AST-allowlisted expression sandbox, in-process.
//!
//! Mirrors `tools.python_eval` + `PYTHON_EVAL_RUNNER`. The oracle shells out to
//! `sys.executable -I -c …`. This port **does not**: it parses, validates and
//! evaluates the same allowlist here. That is the sandbox, not a CPython child.

use serde_json::{Value, json};

use crate::app_error::{AppError, codes};
use crate::memory_index::python_round;
use crate::python_json::float_str;

const MAX_EXPR_CHARS: usize = 1000;
const MAX_RESULT_CHARS: usize = 4000;
const MAX_ERROR_CHARS: usize = 500;
const MAX_INT: i128 = 1_000_000_000_000;
const MAX_CALL_ARGS: usize = 12;

#[derive(Debug, Clone)]
enum Lit {
    None,
    Bool(bool),
    Int(i128),
    Float(f64),
    Str(String),
}

#[derive(Debug, Clone, Copy)]
enum Bin {
    Add,
    Sub,
    Mult,
    Div,
    FloorDiv,
    Mod,
    Pow,
}

#[derive(Debug, Clone, Copy)]
enum Un {
    UAdd,
    USub,
    Not,
}

#[derive(Debug, Clone, Copy)]
enum Cmp {
    Eq,
    NotEq,
    Lt,
    LtE,
    Gt,
    GtE,
}

#[derive(Debug, Clone)]
enum Ast {
    Expression(Box<Ast>),
    Constant(Lit),
    Name(String),
    BinOp(Box<Ast>, Bin, Box<Ast>),
    UnaryOp(Un, Box<Ast>),
    BoolOpAnd(Vec<Ast>),
    BoolOpOr(Vec<Ast>),
    Compare(Box<Ast>, Vec<(Cmp, Ast)>),
    IfExp {
        body: Box<Ast>,
        test: Box<Ast>,
        orelse: Box<Ast>,
    },
    Call {
        func: Box<Ast>,
        args: Vec<Ast>,
        keywords: bool,
    },
    Attribute {
        value: Box<Ast>,
        attr: String,
    },
    Subscript {
        value: Box<Ast>,
        slice: Box<Ast>,
    },
    Slice {
        lower: Option<Box<Ast>>,
        upper: Option<Box<Ast>>,
        step: Option<Box<Ast>>,
    },
    List(Vec<Ast>),
    Tuple(Vec<Ast>),
    Dict {
        keys: Vec<Ast>,
        values: Vec<Ast>,
    },
    Set(Vec<Ast>),
}

impl Ast {
    fn type_name(&self) -> &'static str {
        match self {
            Ast::Expression(_) => "Expression",
            Ast::Constant(_) => "Constant",
            Ast::Name(_) => "Name",
            Ast::BinOp(_, op, _) => match op {
                Bin::Add => "BinOp",
                Bin::Sub => "BinOp",
                Bin::Mult => "BinOp",
                Bin::Div => "BinOp",
                Bin::FloorDiv => "BinOp",
                Bin::Mod => "BinOp",
                Bin::Pow => "BinOp",
            },
            Ast::UnaryOp(Un::Not, _) => "UnaryOp",
            Ast::UnaryOp(_, _) => "UnaryOp",
            Ast::BoolOpAnd(_) | Ast::BoolOpOr(_) => "BoolOp",
            Ast::Compare(_, _) => "Compare",
            Ast::IfExp { .. } => "IfExp",
            Ast::Call { .. } => "Call",
            Ast::Attribute { .. } => "Attribute",
            Ast::Subscript { .. } => "Subscript",
            Ast::Slice { .. } => "Slice",
            Ast::List(_) => "List",
            Ast::Tuple(_) => "Tuple",
            Ast::Dict { .. } => "Dict",
            Ast::Set(_) => "Set",
        }
    }
}

fn allowed_name(name: &str) -> bool {
    matches!(
        name,
        "abs"
            | "round"
            | "min"
            | "max"
            | "sum"
            | "pow"
            | "len"
            | "factorial"
            | "comb"
            | "perm"
            | "gcd"
            | "lcm"
            | "sqrt"
            | "log"
            | "sin"
            | "cos"
            | "tan"
            | "pi"
            | "e"
            | "math"
    )
}

fn callable_name(name: &str) -> bool {
    allowed_name(name) && !matches!(name, "pi" | "e" | "math")
}

fn validate(node: &Ast) -> Result<(), String> {
    match node {
        Ast::Name(id) if !allowed_name(id) => {
            return Err(format!("Unknown name: {id}"));
        }
        Ast::Attribute { value, attr } => {
            let Ast::Name(id) = value.as_ref() else {
                return Err("Only math.<function> attributes are allowed".to_string());
            };
            if id != "math" || attr.starts_with('_') || math_attr(attr).is_none() {
                return Err("Only math.<function> attributes are allowed".to_string());
            }
        }
        Ast::Call {
            func,
            args,
            keywords,
        } => {
            match func.as_ref() {
                Ast::Name(id) => {
                    if !callable_name(id) {
                        return Err(format!("Function is not allowed: {id}"));
                    }
                }
                Ast::Attribute { .. } => validate(func)?,
                _ => return Err("Unsupported function call".to_string()),
            }
            if args.len() > MAX_CALL_ARGS || *keywords {
                return Err("Too many arguments or keyword arguments are not allowed".to_string());
            }
        }
        Ast::Constant(Lit::Int(value)) if value.abs() > MAX_INT => {
            return Err("Integer literal is too large".to_string());
        }
        Ast::UnaryOp(Un::Not, _) => {
            // UnaryOp itself is allowed; the Not operator node is not.
            return Err("Unsupported syntax: Not".to_string());
        }
        _ => {}
    }
    for child in children(node) {
        if !is_allowed(child) {
            return Err(format!("Unsupported syntax: {}", child.type_name()));
        }
        validate(child)?;
    }
    Ok(())
}

fn is_allowed(node: &Ast) -> bool {
    !matches!(node, Ast::UnaryOp(Un::Not, _))
}

fn children(node: &Ast) -> Vec<&Ast> {
    match node {
        Ast::Expression(inner) => vec![inner.as_ref()],
        Ast::BinOp(left, _, right) => vec![left.as_ref(), right.as_ref()],
        Ast::UnaryOp(_, value) => vec![value.as_ref()],
        Ast::BoolOpAnd(values) | Ast::BoolOpOr(values) => values.iter().collect(),
        Ast::Compare(left, rest) => {
            let mut out = vec![left.as_ref()];
            out.extend(rest.iter().map(|(_, value)| value));
            out
        }
        Ast::IfExp { body, test, orelse } => {
            vec![body.as_ref(), test.as_ref(), orelse.as_ref()]
        }
        Ast::Call { func, args, .. } => {
            let mut out = vec![func.as_ref()];
            out.extend(args.iter());
            out
        }
        Ast::Attribute { value, .. } => vec![value.as_ref()],
        Ast::Subscript { value, slice } => vec![value.as_ref(), slice.as_ref()],
        Ast::Slice { lower, upper, step } => {
            let mut out = Vec::new();
            if let Some(node) = lower {
                out.push(node.as_ref());
            }
            if let Some(node) = upper {
                out.push(node.as_ref());
            }
            if let Some(node) = step {
                out.push(node.as_ref());
            }
            out
        }
        Ast::List(items) | Ast::Tuple(items) | Ast::Set(items) => items.iter().collect(),
        Ast::Dict { keys, values } => keys.iter().chain(values.iter()).collect(),
        _ => Vec::new(),
    }
}

#[derive(Debug, Clone, PartialEq)]
enum PyVal {
    None,
    Bool(bool),
    Int(i128),
    Float(f64),
    Str(String),
    List(Vec<PyVal>),
    Tuple(Vec<PyVal>),
    Dict(Vec<(PyVal, PyVal)>),
    Set(Vec<PyVal>),
    Math,
}

fn math_attr(name: &str) -> Option<PyVal> {
    Some(match name {
        "pi" => PyVal::Float(std::f64::consts::PI),
        "e" => PyVal::Float(std::f64::consts::E),
        "tau" => PyVal::Float(std::f64::consts::TAU),
        "inf" => PyVal::Float(f64::INFINITY),
        "nan" => PyVal::Float(f64::NAN),
        "factorial" | "comb" | "perm" | "gcd" | "lcm" | "sqrt" | "log" | "log10" | "log2"
        | "sin" | "cos" | "tan" | "asin" | "acos" | "atan" | "atan2" | "exp" | "floor" | "ceil"
        | "fabs" | "pow" | "hypot" | "degrees" | "radians" | "isfinite" | "isinf" | "isnan" => {
            PyVal::Str(format!("<built-in function {name}>"))
        }
        _ => return None,
    })
}

fn py_repr(value: &PyVal) -> String {
    match value {
        PyVal::None => "None".to_string(),
        PyVal::Bool(true) => "True".to_string(),
        PyVal::Bool(false) => "False".to_string(),
        PyVal::Int(n) => n.to_string(),
        PyVal::Float(n) => {
            if n.is_nan() {
                "nan".to_string()
            } else if *n == f64::INFINITY {
                "inf".to_string()
            } else if *n == f64::NEG_INFINITY {
                "-inf".to_string()
            } else {
                float_str(*n)
            }
        }
        PyVal::Str(text) if text.starts_with("<built-in function ") => text.clone(),
        PyVal::Str(text) => format!("'{}'", text.replace('\\', "\\\\").replace('\'', "\\'")),
        PyVal::List(items) => format!(
            "[{}]",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        PyVal::Tuple(items) if items.len() == 1 => format!("({},)", py_repr(&items[0])),
        PyVal::Tuple(items) => format!(
            "({})",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        PyVal::Dict(items) => format!(
            "{{{}}}",
            items
                .iter()
                .map(|(k, v)| format!("{}: {}", py_repr(k), py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        PyVal::Set(items) if items.is_empty() => "set()".to_string(),
        PyVal::Set(items) => format!(
            "{{{}}}",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        PyVal::Math => "<module 'math'>".to_string(),
    }
}

fn as_bool(value: &PyVal) -> bool {
    match value {
        PyVal::None => false,
        PyVal::Bool(flag) => *flag,
        PyVal::Int(n) => *n != 0,
        PyVal::Float(n) => *n != 0.0 && !n.is_nan(),
        PyVal::Str(text) => !text.is_empty(),
        PyVal::List(items) | PyVal::Tuple(items) | PyVal::Set(items) => !items.is_empty(),
        PyVal::Dict(items) => !items.is_empty(),
        PyVal::Math => true,
    }
}

fn as_float(value: &PyVal) -> Result<f64, String> {
    match value {
        PyVal::Bool(false) => Ok(0.0),
        PyVal::Bool(true) => Ok(1.0),
        PyVal::Int(n) => Ok(*n as f64),
        PyVal::Float(n) => Ok(*n),
        _ => Err("must be real number, not ".to_string()),
    }
}

fn as_int(value: &PyVal) -> Result<i128, String> {
    match value {
        PyVal::Bool(false) => Ok(0),
        PyVal::Bool(true) => Ok(1),
        PyVal::Int(n) => Ok(*n),
        PyVal::Float(n) if *n == n.trunc() && n.is_finite() => Ok(*n as i128),
        _ => Err(format!(
            "'{}' object cannot be interpreted as an integer",
            kind(value)
        )),
    }
}

fn kind(value: &PyVal) -> &'static str {
    match value {
        PyVal::None => "NoneType",
        PyVal::Bool(_) => "bool",
        PyVal::Int(_) => "int",
        PyVal::Float(_) => "float",
        PyVal::Str(_) => "str",
        PyVal::List(_) => "list",
        PyVal::Tuple(_) => "tuple",
        PyVal::Dict(_) => "dict",
        PyVal::Set(_) => "set",
        PyVal::Math => "module",
    }
}

fn numeric_pair(left: &PyVal, right: &PyVal) -> Result<(f64, f64, bool), String> {
    let both_int = matches!(
        (left, right),
        (PyVal::Int(_), PyVal::Int(_))
            | (PyVal::Bool(_), PyVal::Int(_))
            | (PyVal::Int(_), PyVal::Bool(_))
            | (PyVal::Bool(_), PyVal::Bool(_))
    );
    Ok((as_float(left)?, as_float(right)?, both_int))
}

fn eval_binop(op: Bin, left: &PyVal, right: &PyVal) -> Result<PyVal, String> {
    match op {
        Bin::Add => match (left, right) {
            (PyVal::List(a), PyVal::List(b)) => {
                let mut out = a.clone();
                out.extend(b.iter().cloned());
                Ok(PyVal::List(out))
            }
            (PyVal::Tuple(a), PyVal::Tuple(b)) => {
                let mut out = a.clone();
                out.extend(b.iter().cloned());
                Ok(PyVal::Tuple(out))
            }
            (PyVal::Str(a), PyVal::Str(b)) => Ok(PyVal::Str(format!("{a}{b}"))),
            _ => {
                let (l, r, both_int) = numeric_pair(left, right)?;
                if both_int {
                    Ok(PyVal::Int(as_int(left)? + as_int(right)?))
                } else {
                    Ok(PyVal::Float(l + r))
                }
            }
        },
        Bin::Sub => {
            let (l, r, both_int) = numeric_pair(left, right)?;
            if both_int {
                Ok(PyVal::Int(as_int(left)? - as_int(right)?))
            } else {
                Ok(PyVal::Float(l - r))
            }
        }
        Bin::Mult => {
            let (l, r, both_int) = numeric_pair(left, right)?;
            if both_int {
                Ok(PyVal::Int(
                    as_int(left)?
                        .checked_mul(as_int(right)?)
                        .ok_or_else(|| "integer overflow".to_string())?,
                ))
            } else {
                Ok(PyVal::Float(l * r))
            }
        }
        Bin::Div => {
            let (l, r, _) = numeric_pair(left, right)?;
            if r == 0.0 {
                return Err("division by zero".to_string());
            }
            Ok(PyVal::Float(l / r))
        }
        Bin::FloorDiv => {
            let (l, r, both_int) = numeric_pair(left, right)?;
            if r == 0.0 {
                return Err("division by zero".to_string());
            }
            if both_int {
                Ok(PyVal::Int(as_int(left)?.div_euclid(as_int(right)?)))
            } else {
                Ok(PyVal::Float((l / r).floor()))
            }
        }
        Bin::Mod => {
            let (l, r, both_int) = numeric_pair(left, right)?;
            if r == 0.0 {
                return Err("modulo by zero".to_string());
            }
            if both_int {
                Ok(PyVal::Int(as_int(left)?.rem_euclid(as_int(right)?)))
            } else {
                Ok(PyVal::Float(l % r))
            }
        }
        Bin::Pow => {
            let (l, r, both_int) = numeric_pair(left, right)?;
            if both_int {
                let exp = as_int(right)?;
                if exp < 0 {
                    Ok(PyVal::Float(l.powf(r)))
                } else if exp > 64 {
                    Err("integer overflow".to_string())
                } else {
                    Ok(PyVal::Int(
                        as_int(left)?
                            .checked_pow(exp as u32)
                            .ok_or_else(|| "integer overflow".to_string())?,
                    ))
                }
            } else {
                Ok(PyVal::Float(l.powf(r)))
            }
        }
    }
}

fn cmp_values(op: Cmp, left: &PyVal, right: &PyVal) -> Result<bool, String> {
    if let (Ok(l), Ok(r)) = (as_float(left), as_float(right)) {
        return Ok(match op {
            Cmp::Eq => l == r,
            Cmp::NotEq => l != r,
            Cmp::Lt => l < r,
            Cmp::LtE => l <= r,
            Cmp::Gt => l > r,
            Cmp::GtE => l >= r,
        });
    }
    match op {
        Cmp::Eq => Ok(left == right),
        Cmp::NotEq => Ok(left != right),
        _ => Err(format!(
            "'{}' not supported between instances of '{}' and '{}'",
            match op {
                Cmp::Lt => "<",
                Cmp::LtE => "<=",
                Cmp::Gt => ">",
                Cmp::GtE => ">=",
                _ => "==",
            },
            kind(left),
            kind(right)
        )),
    }
}

fn eval_ast(node: &Ast) -> Result<PyVal, String> {
    match node {
        Ast::Expression(inner) => eval_ast(inner),
        Ast::Constant(Lit::None) => Ok(PyVal::None),
        Ast::Constant(Lit::Bool(v)) => Ok(PyVal::Bool(*v)),
        Ast::Constant(Lit::Int(v)) => Ok(PyVal::Int(*v)),
        Ast::Constant(Lit::Float(v)) => Ok(PyVal::Float(*v)),
        Ast::Constant(Lit::Str(v)) => Ok(PyVal::Str(v.clone())),
        Ast::Name(name) => match name.as_str() {
            "pi" => Ok(PyVal::Float(std::f64::consts::PI)),
            "e" => Ok(PyVal::Float(std::f64::consts::E)),
            "math" => Ok(PyVal::Math),
            other if allowed_name(other) => Ok(PyVal::Str(format!("<built-in function {other}>"))),
            other => Err(format!("Unknown name: {other}")),
        },
        Ast::UnaryOp(Un::UAdd, value) => eval_ast(value),
        Ast::UnaryOp(Un::USub, value) => match eval_ast(value)? {
            PyVal::Int(n) => Ok(PyVal::Int(-n)),
            PyVal::Float(n) => Ok(PyVal::Float(-n)),
            PyVal::Bool(false) => Ok(PyVal::Int(0)),
            PyVal::Bool(true) => Ok(PyVal::Int(-1)),
            other => Err(format!("bad operand type for unary -: '{}'", kind(&other))),
        },
        Ast::UnaryOp(Un::Not, _) => Err("Unsupported syntax: Not".to_string()),
        Ast::BinOp(left, op, right) => eval_binop(*op, &eval_ast(left)?, &eval_ast(right)?),
        Ast::BoolOpAnd(values) => {
            let mut last = PyVal::Bool(true);
            for value in values {
                last = eval_ast(value)?;
                if !as_bool(&last) {
                    return Ok(last);
                }
            }
            Ok(last)
        }
        Ast::BoolOpOr(values) => {
            let mut last = PyVal::Bool(false);
            for value in values {
                last = eval_ast(value)?;
                if as_bool(&last) {
                    return Ok(last);
                }
            }
            Ok(last)
        }
        Ast::Compare(left, rest) => {
            let mut current = eval_ast(left)?;
            for (op, rhs) in rest {
                let next = eval_ast(rhs)?;
                if !cmp_values(*op, &current, &next)? {
                    return Ok(PyVal::Bool(false));
                }
                current = next;
            }
            Ok(PyVal::Bool(true))
        }
        Ast::IfExp { body, test, orelse } => {
            if as_bool(&eval_ast(test)?) {
                eval_ast(body)
            } else {
                eval_ast(orelse)
            }
        }
        Ast::Attribute { value, attr } => {
            let PyVal::Math = eval_ast(value)? else {
                return Err("Only math.<function> attributes are allowed".to_string());
            };
            math_attr(attr).ok_or_else(|| "Only math.<function> attributes are allowed".to_string())
        }
        Ast::Call { func, args, .. } => {
            let argv: Vec<PyVal> = args.iter().map(eval_ast).collect::<Result<_, _>>()?;
            match func.as_ref() {
                Ast::Name(name) => call_name(name, &argv),
                Ast::Attribute { attr, .. } => call_math(attr, &argv),
                _ => Err("Unsupported function call".to_string()),
            }
        }
        Ast::List(items) => Ok(PyVal::List(
            items.iter().map(eval_ast).collect::<Result<_, _>>()?,
        )),
        Ast::Tuple(items) => Ok(PyVal::Tuple(
            items.iter().map(eval_ast).collect::<Result<_, _>>()?,
        )),
        Ast::Set(items) => Ok(PyVal::Set(
            items.iter().map(eval_ast).collect::<Result<_, _>>()?,
        )),
        Ast::Dict { keys, values } => {
            let mut out = Vec::new();
            for (key, value) in keys.iter().zip(values.iter()) {
                out.push((eval_ast(key)?, eval_ast(value)?));
            }
            Ok(PyVal::Dict(out))
        }
        Ast::Subscript { value, slice } => eval_subscript(&eval_ast(value)?, slice),
        Ast::Slice { .. } => Err("Unsupported syntax: Slice".to_string()),
    }
}

fn eval_subscript(value: &PyVal, slice: &Ast) -> Result<PyVal, String> {
    if let Ast::Slice { lower, upper, step } = slice {
        let seq = match value {
            PyVal::List(items) | PyVal::Tuple(items) => items,
            PyVal::Str(_) => {
                return Err("string slice is not implemented".to_string());
            }
            _ => {
                return Err(format!(
                    "'{kind}' object is not subscriptable",
                    kind = kind(value)
                ));
            }
        };
        let start = lower.as_ref().map(|n| eval_ast(n)).transpose()?;
        let end = upper.as_ref().map(|n| eval_ast(n)).transpose()?;
        if step.is_some() {
            return Err("slice step is not implemented".to_string());
        }
        let start_i = start.map(|v| as_int(&v)).transpose()?.unwrap_or(0);
        let end_i = end
            .map(|v| as_int(&v))
            .transpose()?
            .unwrap_or(seq.len() as i128);
        let start_u = start_i.max(0) as usize;
        let end_u = end_i.max(0) as usize;
        let sliced: Vec<PyVal> = seq
            .iter()
            .skip(start_u)
            .take(end_u.saturating_sub(start_u))
            .cloned()
            .collect();
        return Ok(match value {
            PyVal::Tuple(_) => PyVal::Tuple(sliced),
            _ => PyVal::List(sliced),
        });
    }
    let key = eval_ast(slice)?;
    match value {
        PyVal::List(items) | PyVal::Tuple(items) => {
            let mut index = as_int(&key)?;
            if index < 0 {
                index += items.len() as i128;
            }
            items
                .get(index as usize)
                .cloned()
                .ok_or_else(|| "list index out of range".to_string())
        }
        PyVal::Dict(items) => items
            .iter()
            .find(|(k, _)| k == &key)
            .map(|(_, v)| v.clone())
            .ok_or_else(|| "key not found".to_string()),
        PyVal::Str(text) => {
            let mut index = as_int(&key)?;
            let chars: Vec<char> = text.chars().collect();
            if index < 0 {
                index += chars.len() as i128;
            }
            chars
                .get(index as usize)
                .map(|ch| PyVal::Str(ch.to_string()))
                .ok_or_else(|| "string index out of range".to_string())
        }
        _ => Err(format!(
            "'{kind}' object is not subscriptable",
            kind = kind(value)
        )),
    }
}

fn call_name(name: &str, args: &[PyVal]) -> Result<PyVal, String> {
    match name {
        "abs" => one(args).and_then(|v| match v {
            PyVal::Int(n) => Ok(PyVal::Int(n.abs())),
            PyVal::Float(n) => Ok(PyVal::Float(n.abs())),
            PyVal::Bool(flag) => Ok(PyVal::Int(i128::from(*flag))),
            _ => Err("bad operand type for abs()".to_string()),
        }),
        "round" => match args.len() {
            1 => Ok(PyVal::Int(python_round(as_float(&args[0])?) as i128)),
            2 => {
                let digits = as_int(&args[1])? as i32;
                let factor = 10f64.powi(digits);
                Ok(PyVal::Float(
                    python_round(as_float(&args[0])? * factor) / factor,
                ))
            }
            _ => Err("round expected at most 2 arguments".to_string()),
        },
        "min" => reduce_ord(args, true),
        "max" => reduce_ord(args, false),
        "sum" => sum_args(args),
        "pow" => match args.len() {
            2 => eval_binop(Bin::Pow, &args[0], &args[1]),
            3 => {
                let base = as_int(&args[0])?;
                let exp = as_int(&args[1])?;
                let modulo = as_int(&args[2])?;
                if exp < 0 {
                    return Err("negative exponent with modulus".to_string());
                }
                Ok(PyVal::Int(base.pow(exp.min(32) as u32) % modulo))
            }
            _ => Err("pow expected 2 arguments".to_string()),
        },
        "len" => one(args).and_then(|v| match v {
            PyVal::Str(text) => Ok(PyVal::Int(text.chars().count() as i128)),
            PyVal::List(items) | PyVal::Tuple(items) | PyVal::Set(items) => {
                Ok(PyVal::Int(items.len() as i128))
            }
            PyVal::Dict(items) => Ok(PyVal::Int(items.len() as i128)),
            _ => Err(format!("object of type '{}' has no len()", kind(v))),
        }),
        "factorial" | "comb" | "perm" | "gcd" | "lcm" | "sqrt" | "log" | "sin" | "cos" | "tan" => {
            call_math(name, args)
        }
        _ => Err(format!("Function is not allowed: {name}")),
    }
}

fn one(args: &[PyVal]) -> Result<&PyVal, String> {
    if args.len() != 1 {
        Err(format!("expected 1 argument, got {}", args.len()))
    } else {
        Ok(&args[0])
    }
}

fn reduce_ord(args: &[PyVal], is_min: bool) -> Result<PyVal, String> {
    let seq: Vec<PyVal> = if args.len() == 1 {
        match &args[0] {
            PyVal::List(items) | PyVal::Tuple(items) | PyVal::Set(items) => items.clone(),
            _ => args.to_vec(),
        }
    } else {
        args.to_vec()
    };
    let Some(first) = seq.first() else {
        return Err("expected at least 1 argument, got 0".to_string());
    };
    let mut best = first.clone();
    for item in seq.iter().skip(1) {
        let take = if is_min {
            cmp_values(Cmp::Lt, item, &best)?
        } else {
            cmp_values(Cmp::Gt, item, &best)?
        };
        if take {
            best = item.clone();
        }
    }
    Ok(best)
}

fn sum_args(args: &[PyVal]) -> Result<PyVal, String> {
    if args.is_empty() {
        return Err("sum expected at least 1 argument, got 0".to_string());
    }
    let start = if args.len() >= 2 {
        args[1].clone()
    } else {
        PyVal::Int(0)
    };
    let seq = match &args[0] {
        PyVal::List(items) | PyVal::Tuple(items) | PyVal::Set(items) => items,
        _ => return Err("sum() can't sum non-iterable".to_string()),
    };
    let mut acc = start;
    for item in seq {
        acc = eval_binop(Bin::Add, &acc, item)?;
    }
    Ok(acc)
}

fn call_math(name: &str, args: &[PyVal]) -> Result<PyVal, String> {
    match name {
        "factorial" => {
            let n = as_int(one(args)?)?;
            if n < 0 {
                return Err("factorial() not defined for negative values".to_string());
            }
            if n > 30 {
                return Err("integer overflow".to_string());
            }
            let mut acc = 1i128;
            for i in 2..=n {
                acc *= i;
            }
            Ok(PyVal::Int(acc))
        }
        "comb" | "perm" => {
            if args.len() != 2 {
                return Err(format!("{name}() takes 2 arguments"));
            }
            let n = as_int(&args[0])?;
            let k = as_int(&args[1])?;
            if n < 0 || k < 0 {
                return Err(format!("{name}() not defined for negative values"));
            }
            if k > n {
                return Ok(PyVal::Int(0));
            }
            let mut acc = 1i128;
            if name == "perm" {
                for i in 0..k {
                    acc = acc
                        .checked_mul(n - i)
                        .ok_or_else(|| "integer overflow".to_string())?;
                }
            } else {
                for i in 0..k {
                    acc = acc
                        .checked_mul(n - i)
                        .ok_or_else(|| "integer overflow".to_string())?
                        / (i + 1);
                }
            }
            Ok(PyVal::Int(acc))
        }
        "gcd" => {
            if args.is_empty() {
                return Ok(PyVal::Int(0));
            }
            let mut acc = as_int(&args[0])?.abs();
            for item in args.iter().skip(1) {
                acc = gcd_i128(acc, as_int(item)?.abs());
            }
            Ok(PyVal::Int(acc))
        }
        "lcm" => {
            if args.is_empty() {
                return Ok(PyVal::Int(1));
            }
            let mut acc = as_int(&args[0])?.abs();
            for item in args.iter().skip(1) {
                let n = as_int(item)?.abs();
                if acc == 0 || n == 0 {
                    acc = 0;
                } else {
                    acc = acc / gcd_i128(acc, n) * n;
                }
            }
            Ok(PyVal::Int(acc))
        }
        "sqrt" => Ok(PyVal::Float(as_float(one(args)?)?.sqrt())),
        "sin" => Ok(PyVal::Float(as_float(one(args)?)?.sin())),
        "cos" => Ok(PyVal::Float(as_float(one(args)?)?.cos())),
        "tan" => Ok(PyVal::Float(as_float(one(args)?)?.tan())),
        "log" => match args.len() {
            1 => Ok(PyVal::Float(as_float(&args[0])?.ln())),
            2 => Ok(PyVal::Float(as_float(&args[0])?.log(as_float(&args[1])?))),
            _ => Err("log expected 1 or 2 arguments".to_string()),
        },
        "log10" => Ok(PyVal::Float(as_float(one(args)?)?.log10())),
        "log2" => Ok(PyVal::Float(as_float(one(args)?)?.log2())),
        "exp" => Ok(PyVal::Float(as_float(one(args)?)?.exp())),
        "floor" => Ok(PyVal::Float(as_float(one(args)?)?.floor())),
        "ceil" => Ok(PyVal::Float(as_float(one(args)?)?.ceil())),
        "fabs" => Ok(PyVal::Float(as_float(one(args)?)?.abs())),
        "pow" => {
            if args.len() != 2 {
                return Err("pow expected 2 arguments".to_string());
            }
            Ok(PyVal::Float(as_float(&args[0])?.powf(as_float(&args[1])?)))
        }
        "hypot" => {
            if args.len() != 2 {
                return Err("hypot expected 2 arguments".to_string());
            }
            Ok(PyVal::Float(as_float(&args[0])?.hypot(as_float(&args[1])?)))
        }
        "atan2" => {
            if args.len() != 2 {
                return Err("atan2 expected 2 arguments".to_string());
            }
            Ok(PyVal::Float(as_float(&args[0])?.atan2(as_float(&args[1])?)))
        }
        "degrees" => Ok(PyVal::Float(as_float(one(args)?)?.to_degrees())),
        "radians" => Ok(PyVal::Float(as_float(one(args)?)?.to_radians())),
        "asin" => Ok(PyVal::Float(as_float(one(args)?)?.asin())),
        "acos" => Ok(PyVal::Float(as_float(one(args)?)?.acos())),
        "atan" => Ok(PyVal::Float(as_float(one(args)?)?.atan())),
        other => Err(format!(
            "Only math.<function> attributes are allowed ({other})"
        )),
    }
}

fn gcd_i128(mut a: i128, mut b: i128) -> i128 {
    while b != 0 {
        let t = b;
        b = a % b;
        a = t;
    }
    a.abs()
}

#[derive(Debug, Clone)]
enum Tok {
    Number(String),
    String(String),
    Name(String),
    Sym(&'static str),
    Eof,
}

struct Lexer<'a> {
    src: &'a [u8],
    i: usize,
}

impl<'a> Lexer<'a> {
    fn new(src: &'a str) -> Self {
        Self {
            src: src.as_bytes(),
            i: 0,
        }
    }

    fn peek(&self) -> Option<u8> {
        self.src.get(self.i).copied()
    }

    fn bump(&mut self) -> Option<u8> {
        let ch = self.peek()?;
        self.i += 1;
        Some(ch)
    }

    fn next(&mut self) -> Result<Tok, String> {
        while let Some(b' ' | b'\t' | b'\n' | b'\r') = self.peek() {
            self.bump();
        }
        let Some(ch) = self.peek() else {
            return Ok(Tok::Eof);
        };
        if ch.is_ascii_digit()
            || (ch == b'.' && self.src.get(self.i + 1).is_some_and(u8::is_ascii_digit))
        {
            return self.number();
        }
        if ch == b'\'' || ch == b'"' {
            return self.string(ch);
        }
        if ch.is_ascii_alphabetic() || ch == b'_' {
            let start = self.i;
            while self
                .peek()
                .is_some_and(|c| c.is_ascii_alphanumeric() || c == b'_')
            {
                self.bump();
            }
            let name = std::str::from_utf8(&self.src[start..self.i]).unwrap();
            return Ok(Tok::Name(name.to_string()));
        }
        self.i += 1;
        let two = self.peek();
        let sym = match (ch, two) {
            (b'*', Some(b'*')) => {
                self.i += 1;
                "**"
            }
            (b'/', Some(b'/')) => {
                self.i += 1;
                "//"
            }
            (b'=', Some(b'=')) => {
                self.i += 1;
                "=="
            }
            (b'!', Some(b'=')) => {
                self.i += 1;
                "!="
            }
            (b'<', Some(b'=')) => {
                self.i += 1;
                "<="
            }
            (b'>', Some(b'=')) => {
                self.i += 1;
                ">="
            }
            (b'+', _) => "+",
            (b'-', _) => "-",
            (b'*', _) => "*",
            (b'/', _) => "/",
            (b'%', _) => "%",
            (b'(', _) => "(",
            (b')', _) => ")",
            (b'[', _) => "[",
            (b']', _) => "]",
            (b'{', _) => "{",
            (b'}', _) => "}",
            (b',', _) => ",",
            (b':', _) => ":",
            (b'.', _) => ".",
            (b'<', _) => "<",
            (b'>', _) => ">",
            _ => return Err("invalid syntax".to_string()),
        };
        Ok(Tok::Sym(sym))
    }

    fn number(&mut self) -> Result<Tok, String> {
        let start = self.i;
        while self.peek().is_some_and(|c| c.is_ascii_digit()) {
            self.bump();
        }
        if self.peek() == Some(b'.') {
            self.bump();
            while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                self.bump();
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.bump();
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.bump();
            }
            while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                self.bump();
            }
        }
        Ok(Tok::Number(
            std::str::from_utf8(&self.src[start..self.i])
                .unwrap()
                .to_string(),
        ))
    }

    fn string(&mut self, quote: u8) -> Result<Tok, String> {
        self.bump();
        let mut out = String::new();
        loop {
            match self.bump() {
                None => return Err("unterminated string literal".to_string()),
                Some(ch) if ch == quote => break,
                Some(b'\\') => match self.bump() {
                    Some(b'n') => out.push('\n'),
                    Some(b't') => out.push('\t'),
                    Some(b'\\') => out.push('\\'),
                    Some(b'\'') => out.push('\''),
                    Some(b'"') => out.push('"'),
                    Some(other) => out.push(other as char),
                    None => return Err("unterminated string literal".to_string()),
                },
                Some(ch) => out.push(ch as char),
            }
        }
        Ok(Tok::String(out))
    }
}

struct Parser {
    tokens: Vec<Tok>,
    i: usize,
}

impl Parser {
    fn parse(source: &str) -> Result<Ast, String> {
        let mut lexer = Lexer::new(source);
        let mut tokens = Vec::new();
        loop {
            let tok = lexer.next()?;
            let done = matches!(tok, Tok::Eof);
            tokens.push(tok);
            if done {
                break;
            }
        }
        let mut parser = Self { tokens, i: 0 };
        let expr = parser.parse_tuple()?;
        if !matches!(parser.cur(), Tok::Eof) {
            return Err("invalid syntax".to_string());
        }
        Ok(Ast::Expression(Box::new(expr)))
    }

    fn cur(&self) -> &Tok {
        self.tokens.get(self.i).unwrap_or(&Tok::Eof)
    }

    fn bump(&mut self) -> Tok {
        let tok = self.tokens.get(self.i).cloned().unwrap_or(Tok::Eof);
        if self.i < self.tokens.len() {
            self.i += 1;
        }
        tok
    }

    fn eat(&mut self, sym: &str) -> bool {
        if matches!(self.cur(), Tok::Sym(s) if *s == sym) {
            self.bump();
            true
        } else {
            false
        }
    }

    fn parse_tuple(&mut self) -> Result<Ast, String> {
        let first = self.parse_if()?;
        if !self.eat(",") {
            return Ok(first);
        }
        let mut items = vec![first];
        if !matches!(self.cur(), Tok::Eof | Tok::Sym(")" | "]" | "}" | ":")) {
            loop {
                items.push(self.parse_if()?);
                if !self.eat(",") {
                    break;
                }
                if matches!(self.cur(), Tok::Eof | Tok::Sym(")" | "]" | "}" | ":")) {
                    break;
                }
            }
        }
        Ok(Ast::Tuple(items))
    }

    fn parse_if(&mut self) -> Result<Ast, String> {
        let body = self.parse_or()?;
        if matches!(self.cur(), Tok::Name(name) if name == "if") {
            self.bump();
            let test = self.parse_or()?;
            if !matches!(self.cur(), Tok::Name(name) if name == "else") {
                return Err("invalid syntax".to_string());
            }
            self.bump();
            let orelse = self.parse_if()?;
            return Ok(Ast::IfExp {
                body: Box::new(body),
                test: Box::new(test),
                orelse: Box::new(orelse),
            });
        }
        Ok(body)
    }

    fn parse_or(&mut self) -> Result<Ast, String> {
        let mut values = vec![self.parse_and()?];
        while matches!(self.cur(), Tok::Name(name) if name == "or") {
            self.bump();
            values.push(self.parse_and()?);
        }
        if values.len() == 1 {
            Ok(values.pop().unwrap())
        } else {
            Ok(Ast::BoolOpOr(values))
        }
    }

    fn parse_and(&mut self) -> Result<Ast, String> {
        let mut values = vec![self.parse_not()?];
        while matches!(self.cur(), Tok::Name(name) if name == "and") {
            self.bump();
            values.push(self.parse_not()?);
        }
        if values.len() == 1 {
            Ok(values.pop().unwrap())
        } else {
            Ok(Ast::BoolOpAnd(values))
        }
    }

    fn parse_not(&mut self) -> Result<Ast, String> {
        if matches!(self.cur(), Tok::Name(name) if name == "not") {
            self.bump();
            return Ok(Ast::UnaryOp(Un::Not, Box::new(self.parse_not()?)));
        }
        self.parse_cmp()
    }

    fn parse_cmp(&mut self) -> Result<Ast, String> {
        let left = self.parse_add()?;
        let mut rest = Vec::new();
        loop {
            let op = match self.cur() {
                Tok::Sym("==") => Cmp::Eq,
                Tok::Sym("!=") => Cmp::NotEq,
                Tok::Sym("<") => Cmp::Lt,
                Tok::Sym("<=") => Cmp::LtE,
                Tok::Sym(">") => Cmp::Gt,
                Tok::Sym(">=") => Cmp::GtE,
                _ => break,
            };
            self.bump();
            rest.push((op, self.parse_add()?));
        }
        if rest.is_empty() {
            Ok(left)
        } else {
            Ok(Ast::Compare(Box::new(left), rest))
        }
    }

    fn parse_add(&mut self) -> Result<Ast, String> {
        let mut left = self.parse_mul()?;
        loop {
            let op = if self.eat("+") {
                Bin::Add
            } else if self.eat("-") {
                Bin::Sub
            } else {
                break;
            };
            left = Ast::BinOp(Box::new(left), op, Box::new(self.parse_mul()?));
        }
        Ok(left)
    }

    fn parse_mul(&mut self) -> Result<Ast, String> {
        let mut left = self.parse_unary()?;
        loop {
            let op = if self.eat("*") {
                Bin::Mult
            } else if self.eat("/") {
                Bin::Div
            } else if self.eat("//") {
                Bin::FloorDiv
            } else if self.eat("%") {
                Bin::Mod
            } else {
                break;
            };
            left = Ast::BinOp(Box::new(left), op, Box::new(self.parse_unary()?));
        }
        Ok(left)
    }

    fn parse_unary(&mut self) -> Result<Ast, String> {
        if self.eat("+") {
            return Ok(Ast::UnaryOp(Un::UAdd, Box::new(self.parse_unary()?)));
        }
        if self.eat("-") {
            return Ok(Ast::UnaryOp(Un::USub, Box::new(self.parse_unary()?)));
        }
        self.parse_pow()
    }

    fn parse_pow(&mut self) -> Result<Ast, String> {
        let left = self.parse_postfix()?;
        if self.eat("**") {
            Ok(Ast::BinOp(
                Box::new(left),
                Bin::Pow,
                Box::new(self.parse_unary()?),
            ))
        } else {
            Ok(left)
        }
    }

    fn parse_postfix(&mut self) -> Result<Ast, String> {
        let mut value = self.parse_primary()?;
        loop {
            if self.eat(".") {
                let Tok::Name(attr) = self.bump() else {
                    return Err("invalid syntax".to_string());
                };
                value = Ast::Attribute {
                    value: Box::new(value),
                    attr,
                };
            } else if self.eat("(") {
                let mut args = Vec::new();
                let mut keywords = false;
                if !self.eat(")") {
                    loop {
                        if matches!(self.cur(), Tok::Name(_)) {
                            let save = self.i;
                            if let Tok::Name(_) = self.bump() {
                                if self.eat("=") {
                                    keywords = true;
                                    let _ = self.parse_if()?;
                                } else {
                                    self.i = save;
                                    args.push(self.parse_if()?);
                                }
                            }
                        } else {
                            args.push(self.parse_if()?);
                        }
                        if self.eat(",") {
                            continue;
                        }
                        break;
                    }
                    if !self.eat(")") {
                        return Err("invalid syntax".to_string());
                    }
                }
                value = Ast::Call {
                    func: Box::new(value),
                    args,
                    keywords,
                };
            } else if self.eat("[") {
                let slice = self.parse_slice()?;
                if !self.eat("]") {
                    return Err("invalid syntax".to_string());
                }
                value = Ast::Subscript {
                    value: Box::new(value),
                    slice: Box::new(slice),
                };
            } else {
                break;
            }
        }
        Ok(value)
    }

    fn parse_slice(&mut self) -> Result<Ast, String> {
        if self.eat(":") {
            let upper = if matches!(self.cur(), Tok::Sym("]" | ":")) {
                None
            } else {
                Some(Box::new(self.parse_if()?))
            };
            let step = if self.eat(":") {
                if matches!(self.cur(), Tok::Sym("]")) {
                    None
                } else {
                    Some(Box::new(self.parse_if()?))
                }
            } else {
                None
            };
            return Ok(Ast::Slice {
                lower: None,
                upper,
                step,
            });
        }
        let first = self.parse_tuple()?;
        if self.eat(":") {
            let upper = if matches!(self.cur(), Tok::Sym("]" | ":")) {
                None
            } else {
                Some(Box::new(self.parse_if()?))
            };
            let step = if self.eat(":") {
                if matches!(self.cur(), Tok::Sym("]")) {
                    None
                } else {
                    Some(Box::new(self.parse_if()?))
                }
            } else {
                None
            };
            return Ok(Ast::Slice {
                lower: Some(Box::new(first)),
                upper,
                step,
            });
        }
        Ok(first)
    }

    fn parse_primary(&mut self) -> Result<Ast, String> {
        match self.bump() {
            Tok::Number(text) => parse_number(&text),
            Tok::String(text) => Ok(Ast::Constant(Lit::Str(text))),
            Tok::Name(name) if name == "True" => Ok(Ast::Constant(Lit::Bool(true))),
            Tok::Name(name) if name == "False" => Ok(Ast::Constant(Lit::Bool(false))),
            Tok::Name(name) if name == "None" => Ok(Ast::Constant(Lit::None)),
            Tok::Name(name) => Ok(Ast::Name(name)),
            Tok::Sym("(") => {
                if self.eat(")") {
                    return Ok(Ast::Tuple(Vec::new()));
                }
                let inner = self.parse_tuple()?;
                if !self.eat(")") {
                    return Err("invalid syntax".to_string());
                }
                Ok(inner)
            }
            Tok::Sym("[") => {
                let mut items = Vec::new();
                if !self.eat("]") {
                    loop {
                        items.push(self.parse_if()?);
                        if self.eat(",") {
                            if matches!(self.cur(), Tok::Sym("]")) {
                                break;
                            }
                            continue;
                        }
                        break;
                    }
                    if !self.eat("]") {
                        return Err("invalid syntax".to_string());
                    }
                }
                Ok(Ast::List(items))
            }
            Tok::Sym("{") => self.parse_brace(),
            _ => Err("invalid syntax".to_string()),
        }
    }

    fn parse_brace(&mut self) -> Result<Ast, String> {
        if self.eat("}") {
            return Ok(Ast::Dict {
                keys: Vec::new(),
                values: Vec::new(),
            });
        }
        let first = self.parse_if()?;
        if self.eat(":") {
            let mut keys = vec![first];
            let mut values = vec![self.parse_if()?];
            while self.eat(",") {
                if matches!(self.cur(), Tok::Sym("}")) {
                    break;
                }
                keys.push(self.parse_if()?);
                if !self.eat(":") {
                    return Err("invalid syntax".to_string());
                }
                values.push(self.parse_if()?);
            }
            if !self.eat("}") {
                return Err("invalid syntax".to_string());
            }
            Ok(Ast::Dict { keys, values })
        } else {
            let mut items = vec![first];
            while self.eat(",") {
                if matches!(self.cur(), Tok::Sym("}")) {
                    break;
                }
                items.push(self.parse_if()?);
            }
            if !self.eat("}") {
                return Err("invalid syntax".to_string());
            }
            Ok(Ast::Set(items))
        }
    }
}

fn parse_number(text: &str) -> Result<Ast, String> {
    if text.contains('.') || text.contains('e') || text.contains('E') {
        let value: f64 = text.parse().map_err(|_| "invalid syntax".to_string())?;
        Ok(Ast::Constant(Lit::Float(value)))
    } else {
        let value: i128 = text
            .parse()
            .map_err(|_| "Integer literal is too large".to_string())?;
        Ok(Ast::Constant(Lit::Int(value)))
    }
}

fn take_chars(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

fn run_runner(expression: &str) -> Result<String, String> {
    let parsed = Parser::parse(expression)?;
    validate(&parsed)?;
    let value = eval_ast(&parsed)?;
    Ok(take_chars(&py_repr(&value), MAX_RESULT_CHARS))
}

/// Mirrors `python_eval`.
pub fn python_eval(expression: &str) -> Result<Value, AppError> {
    let expression = expression.trim();
    if expression.is_empty() {
        return Err(AppError {
            message: "python_eval expression is empty".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    if expression.chars().count() > MAX_EXPR_CHARS {
        return Err(AppError {
            message: "python_eval expression is too long".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    match run_runner(expression) {
        Ok(result) => Ok(json!({"expression": expression, "result": result})),
        Err(error) => Err(AppError {
            message: take_chars(&error, MAX_ERROR_CHARS),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn factorial_and_arithmetic_match_the_oracle() {
        let result = python_eval("factorial(6)").unwrap();
        assert_eq!(result["result"], "720");
        let result = python_eval("1 + 2 * 3").unwrap();
        assert_eq!(result["result"], "7");
        let result = python_eval("2+2").unwrap();
        assert_eq!(result["result"], "4");
    }

    #[test]
    fn unsafe_and_empty_are_invalid_payload() {
        let err = python_eval("__import__('os').system('whoami')").unwrap_err();
        assert_eq!(err.code, codes::INVALID_PAYLOAD);
        let err = python_eval("").unwrap_err();
        assert_eq!(err.message, "python_eval expression is empty");
        let err = python_eval(&"1".repeat(1001)).unwrap_err();
        assert_eq!(err.message, "python_eval expression is too long");
    }

    #[test]
    fn math_attribute_and_compare_work() {
        let result = python_eval("math.sqrt(4)").unwrap();
        assert_eq!(result["result"], "2.0");
        let result = python_eval("1 < 2 < 3").unwrap();
        assert_eq!(result["result"], "True");
        let result = python_eval("min(3, 1, 2)").unwrap();
        assert_eq!(result["result"], "1");
    }
}
