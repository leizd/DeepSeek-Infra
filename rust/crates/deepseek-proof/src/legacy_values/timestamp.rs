//! Legacy `datetime.fromisoformat(...replace("Z", "+00:00"))` timestamps.
//! Compare integer microseconds, never floating point or the host's local timezone.

const DAY: i64 = 86_400_000_000;
const MAX_ORDINAL: i64 = 3_652_059;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ParsedTimestamp {
    Aware(i64),
    Naive,
    Invalid,
}

pub(crate) fn parse_timestamp(value: &str) -> ParsedTimestamp {
    parse(value).unwrap_or(ParsedTimestamp::Invalid)
}

fn parse(value: &str) -> Option<ParsedTimestamp> {
    let value = value.replace('Z', "+00:00");
    let characters: Vec<_> = value.chars().collect();
    let separator = date_separator(&characters)?;
    let date: String = characters.get(..separator)?.iter().collect();
    let ordinal = date_ordinal(date.as_bytes())?;
    if separator == characters.len() {
        return Some(ParsedTimestamp::Naive);
    }
    // CPython accepts any single Unicode date/time separator.
    let time: String = characters.get(separator + 1..)?.iter().collect();
    let zone_index = time.find(['+', '-']);
    let clock = zone_index.map_or(time.as_str(), |index| &time[..index]);
    let [hour, minute, second, fraction] = time_parts(clock.as_bytes())?;
    if hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    let Some(index) = zone_index else {
        return Some(ParsedTimestamp::Naive);
    };
    let [zh, zm, zs, zf] = time_parts(time.get(index + 1..)?.as_bytes())?;
    let mut offset = ((zh * 60 + zm) * 60 + zs) * 1_000_000;
    // The C reference treats a zero-second offset as UTC, even with a fraction.
    if offset != 0 {
        offset += zf;
    }
    if offset >= DAY {
        return None;
    }
    if time.as_bytes()[index] == b'-' {
        offset = -offset;
    }
    let instant =
        (ordinal - 1) * DAY + ((hour * 60 + minute) * 60 + second) * 1_000_000 + fraction - offset;
    // UTC normalization outside Python's datetime range cannot produce valid evidence.
    (0..MAX_ORDINAL * DAY)
        .contains(&instant)
        .then_some(ParsedTimestamp::Aware(instant))
}

fn date_separator(value: &[char]) -> Option<usize> {
    if value.len() < 7 {
        return None;
    }
    if value.len() == 7 {
        return Some(7);
    }
    if value[4] == '-' {
        return Some(if value[5] != 'W' {
            10
        } else if value.get(8) == Some(&'-') {
            if value.get(10).is_some_and(char::is_ascii_digit) {
                8
            } else {
                10
            }
        } else {
            8
        });
    }
    if value[4] != 'W' {
        return Some(8);
    }
    let end = (7..value.len())
        .find(|index| !value[*index].is_ascii_digit())
        .unwrap_or(value.len());
    Some(if end < 9 {
        end
    } else if end % 2 == 0 {
        7
    } else {
        8
    })
}

fn decimal(value: &[u8]) -> Option<i64> {
    if value.is_empty() || !value.iter().all(u8::is_ascii_digit) {
        return None;
    }
    Some(
        value
            .iter()
            .fold(0, |number, digit| number * 10 + i64::from(digit - b'0')),
    )
}

fn date_ordinal(value: &[u8]) -> Option<i64> {
    let year = decimal(value.get(..4)?)?;
    if !(1..=9999).contains(&year) {
        return None;
    }
    let separated = value.get(4) == Some(&b'-');
    let mut position = 4 + usize::from(separated);
    if value.get(position) == Some(&b'W') {
        position += 1;
        let week = decimal(value.get(position..position + 2)?)?;
        position += 2;
        let day = if position == value.len() {
            1
        } else {
            if separated {
                if value.get(position) != Some(&b'-') {
                    return None;
                }
                position += 1;
            }
            if value.len() != position + 1 {
                return None;
            }
            decimal(value.get(position..position + 1)?)?
        };
        let january_first = days_before_year(year) + 1;
        let weekday = (january_first - 1) % 7;
        let max_week = if weekday == 3 || (weekday == 2 && leap(year)) {
            53
        } else {
            52
        };
        if !(1..=max_week).contains(&week) || !(1..=7).contains(&day) {
            return None;
        }
        let january_fourth = january_first + 3;
        let ordinal = january_fourth - (january_fourth - 1) % 7 + (week - 1) * 7 + day - 1;
        return (1..=MAX_ORDINAL).contains(&ordinal).then_some(ordinal);
    }
    let month = decimal(value.get(position..position + 2)?)?;
    position += 2;
    if separated {
        if value.get(position) != Some(&b'-') {
            return None;
        }
        position += 1;
    }
    if value.len() != position + 2 || !(1..=12).contains(&month) {
        return None;
    }
    let day = decimal(value.get(position..position + 2)?)?;
    let months = [
        31,
        if leap(year) { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    if !(1..=months[(month - 1) as usize]).contains(&day) {
        return None;
    }
    Some(days_before_year(year) + months[..(month - 1) as usize].iter().sum::<i64>() + day)
}

fn leap(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

fn days_before_year(year: i64) -> i64 {
    let previous = year - 1;
    previous * 365 + previous / 4 - previous / 100 + previous / 400
}

fn time_parts(value: &[u8]) -> Option<[i64; 4]> {
    let mut parts = [0; 4];
    let mut position = 0;
    let separated = value.get(2) == Some(&b':');
    for (index, part) in parts[..3].iter_mut().enumerate() {
        *part = decimal(value.get(position..position + 2)?)?;
        position += 2;
        if position == value.len() || matches!(value.get(position), Some(b'.' | b',')) || index == 2
        {
            break;
        }
        if separated {
            if value.get(position) != Some(&b':') {
                return None;
            }
            position += 1;
        }
    }
    if position < value.len() {
        if !matches!(value.get(position), Some(b'.' | b',')) {
            return None;
        }
        let fraction = value.get(position + 1..)?;
        if fraction.is_empty() || !fraction.iter().all(u8::is_ascii_digit) {
            return None;
        }
        let count = fraction.len().min(6);
        parts[3] = decimal(&fraction[..count])? * 10_i64.pow((6 - count) as u32);
    }
    Some(parts)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_preserve_microsecond_order_and_invalid_boundaries() {
        let base = parse_timestamp("2026-08-28T12:00:10.123456Z");
        assert!(matches!(base, ParsedTimestamp::Aware(_)));
        for same in [
            "20260828T120010.123456+00",
            "2026-W35-5🦀20:00:10,123456+08:00",
            "2026W355T120010.123456999+0000",
            "2026-08-28T12:00:40.123456+00:00:30",
        ] {
            assert_eq!(parse_timestamp(same), base, "{same}");
        }
        assert_eq!(
            parse_timestamp("2026-08-28T12:00:10"),
            ParsedTimestamp::Naive
        );
        for invalid in [
            "",
            "2026-02-29T12:00Z",
            "2025-W53-1T12:00Z",
            "2026-08-28T12:00+24:00",
            "2026-08-28T12:00.Z",
            "0001-01-01T00:00+01:00",
            "9999-12-31T23:59-01:00",
        ] {
            assert_eq!(
                parse_timestamp(invalid),
                ParsedTimestamp::Invalid,
                "{invalid}"
            );
        }
    }
}
