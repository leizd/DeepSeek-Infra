//! Binary64 conversion for already validated decimal tokens.

pub(super) fn parse(token: &str) -> Result<f64, ()> {
    let Some((mantissa, exponent)) = token.split_once(['e', 'E']) else {
        return token.parse().map_err(|_| ());
    };
    // CPython 3.12 clips an exponent with over nine significant digits.
    // See Python/dtoa.c in CPython v3.12.12.
    let negative_exponent = exponent.starts_with('-');
    let digits = exponent
        .trim_start_matches(['+', '-'])
        .trim_start_matches('0');
    let magnitude = if digits.len() > 9 {
        1_100_000_000
    } else {
        digits.parse::<i128>().unwrap_or(0)
    };
    let exponent = if negative_exponent {
        -magnitude
    } else {
        magnitude
    };
    if exponent.unsigned_abs() < 65_536 {
        return token.parse().map_err(|_| ());
    }
    // Rust 1.85 stops accumulating long explicit exponents at 65536.
    // Resolve the decimal position first so a long mantissa can cancel it.
    let (sign, mantissa) = mantissa
        .strip_prefix('-')
        .map_or(("", mantissa), |rest| ("-", rest));
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let digits = format!("{whole}{fraction}");
    let significant = digits.trim_start_matches('0');
    let signed_zero = if sign.is_empty() { 0.0 } else { -0.0 };
    if significant.is_empty() {
        return Ok(signed_zero);
    }
    let leading = digits.len() - significant.len();
    let adjusted = exponent.saturating_add(whole.len() as i128 - leading as i128 - 1);
    if adjusted > 308 {
        return Ok(if sign.is_empty() {
            f64::INFINITY
        } else {
            f64::NEG_INFINITY
        });
    }
    if adjusted < -324 {
        return Ok(signed_zero);
    }
    let significant = significant.trim_end_matches('0');
    let (first, fraction) = significant.split_at(1);
    let normalized = if fraction.is_empty() {
        format!("{sign}{first}e{adjusted}")
    } else {
        format!("{sign}{first}.{fraction}e{adjusted}")
    };
    normalized.parse().map_err(|_| ())
}

/// Python repr uses a ties-to-even shortest decimal, with fixed notation for
/// decimal exponents -4 through 15 and a decimal point on integral floats.
pub(super) fn text(value: f64) -> String {
    if !value.is_finite() || value == 0.0 {
        return crate::python_json::float_str(value);
    }
    let mut buffer = ryu::Buffer::new();
    let shortest = buffer.format_finite(value.abs());
    let (mantissa, exponent) =
        shortest
            .split_once('e')
            .map_or((shortest, 0), |(mantissa, exponent)| {
                (
                    mantissa,
                    exponent.parse::<i32>().expect("Ryu decimal exponent"),
                )
            });
    let whole_length = mantissa.find('.').unwrap_or(mantissa.len());
    let digits = mantissa.replace('.', "");
    let significant = digits.trim_start_matches('0');
    let leading = digits.len() - significant.len();
    let decimal = whole_length as i32 + exponent - leading as i32;
    let significant = significant.trim_end_matches('0');
    let mut result = String::new();
    if value.is_sign_negative() {
        result.push('-');
    }
    if !(-3..=16).contains(&decimal) {
        let (first, fraction) = significant.split_at(1);
        result.push_str(first);
        if !fraction.is_empty() {
            result.push('.');
            result.push_str(fraction);
        }
        let exponent = decimal - 1;
        let sign = if exponent < 0 { '-' } else { '+' };
        result.push_str(&format!("e{sign}{:02}", exponent.unsigned_abs()));
    } else if decimal <= 0 {
        result.push_str("0.");
        result.extend(std::iter::repeat_n('0', (-decimal) as usize));
        result.push_str(significant);
    } else {
        let decimal = decimal as usize;
        if decimal >= significant.len() {
            result.push_str(significant);
            result.extend(std::iter::repeat_n('0', decimal - significant.len()));
            result.push_str(".0");
        } else {
            let (whole, fraction) = significant.split_at(decimal);
            result.push_str(whole);
            result.push('.');
            result.push_str(fraction);
        }
    }
    result
}
