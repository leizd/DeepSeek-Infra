//! python_eval parity probe, Rust side.

use deepseek_policy::python_eval::python_eval;
use serde_json::{Map, Value, json};

fn view(expression: &str) -> Value {
    match python_eval(expression) {
        Ok(result) => json!({"ok": true, "result": result}),
        Err(error) => json!({
            "ok": false,
            "error": error.message,
            "code": error.code,
            "status": error.status
        }),
    }
}

fn main() {
    let cases = [
        "factorial(6)",
        "1 + 2 * 3",
        "2+2",
        "math.sqrt(4)",
        "1 < 2 < 3",
        "min(3, 1, 2)",
        "max([1, 8, 3])",
        "sum([1, 2, 3])",
        "pow(2, 10)",
        "round(2.5)",
        "abs(-7)",
        "len([1, 2, 3])",
        "True and 0 or 4",
        "1 if 0 else 2",
        "(1, 2)",
        "[1, 2][0]",
        "gcd(12, 8)",
        "comb(5, 2)",
        "math.pi > 3",
        "",
        &"1".repeat(1001),
        "__import__('os').system('whoami')",
        "unknown_name",
        "pi()",
        "open('x')",
        "1 / 0",
    ];
    let mut out = Map::new();
    for (index, expression) in cases.iter().enumerate() {
        let key = format!(
            "case::{index}::{}",
            expression.chars().take(40).collect::<String>()
        );
        out.insert(key, view(expression));
    }
    let mut encoded = serde_json::to_string_pretty(&Value::Object(out)).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}
