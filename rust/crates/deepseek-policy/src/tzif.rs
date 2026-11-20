//! Minimal pure-Rust RFC 8536 TZif + POSIX TZ rule resolver on top of `chrono`.
//!
//! Mirrors Python's `zoneinfo.ZoneInfo(name)` for retention timezone bucketing
//! without requiring external `Cargo.lock` crate additions.

use std::fs;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Datelike, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, Timelike, Utc};

use crate::app_error::{AppError, codes};

#[derive(Debug, Clone, PartialEq, Eq)]
struct Transition {
    utc_seconds: i64,
    offset_seconds: i32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum DstDateRule {
    MonthWeekDay { month: u32, week: u32, weekday: u32 },
    JulianNoLeap(u32),
    JulianZero(u32),
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct DstTransitionSpec {
    date: DstDateRule,
    time_seconds: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct PosixDst {
    dst_offset: i32,
    start: DstTransitionSpec,
    end: DstTransitionSpec,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PosixTzRule {
    std_offset: i32,
    dst: Option<PosixDst>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IanaTimeZone {
    name: String,
    fixed_offset: Option<i32>,
    transitions: Vec<Transition>,
    default_offset: i32,
    posix_tail: Option<PosixTzRule>,
}

fn unknown_tz(name: &str) -> AppError {
    AppError {
        message: format!("Unknown IANA timezone: {name}"),
        code: codes::INVALID_PAYLOAD,
        status: 400,
    }
}

fn valid_tz_key(name: &str) -> bool {
    if name.is_empty()
        || name.starts_with('/')
        || name.ends_with('/')
        || name.contains('\\')
        || name.contains('\0')
    {
        return false;
    }
    !name
        .split('/')
        .any(|segment| segment.is_empty() || segment == "." || segment == "..")
}

/// Resolve an IANA timezone matching `backup_cron.load_timezone(name)`.
pub fn load_iana_timezone(name: &str) -> Result<IanaTimeZone, AppError> {
    if !valid_tz_key(name) {
        return Err(unknown_tz(name));
    }
    if let Some(fixed) = builtin_fixed_offset(name) {
        return Ok(IanaTimeZone {
            name: name.to_string(),
            fixed_offset: Some(fixed),
            transitions: Vec::new(),
            default_offset: fixed,
            posix_tail: None,
        });
    }
    for root in zoneinfo_roots() {
        let candidate = root.join(name);
        if candidate.is_file() {
            let Ok(bytes) = fs::read(&candidate) else {
                continue;
            };
            if let Some(tz) = parse_tzif(name, &bytes) {
                return Ok(tz);
            }
        }
    }
    Err(unknown_tz(name))
}

fn builtin_fixed_offset(name: &str) -> Option<i32> {
    match name {
        "UTC" | "Etc/UTC" | "Etc/UCT" | "UCT" | "GMT" | "Etc/GMT" | "Etc/GMT+0" | "Etc/GMT-0" => {
            Some(0)
        }
        _ => {
            let rest = name.strip_prefix("Etc/GMT")?;
            let sign_char = rest.chars().next()?;
            let digits = &rest[sign_char.len_utf8()..];
            if digits.is_empty() || !digits.chars().all(|c| c.is_ascii_digit()) {
                return None;
            }
            let hours: i32 = digits.parse().ok()?;
            // IANA Etc/GMT signs follow POSIX convention: Etc/GMT-5 == UTC+5.
            match sign_char {
                '+' if hours <= 12 => Some(-hours * 3600),
                '-' if hours <= 14 => Some(hours * 3600),
                _ => None,
            }
        }
    }
}

fn zoneinfo_roots() -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Some(tzdir) = std::env::var_os("TZDIR") {
        if !tzdir.is_empty() {
            roots.push(PathBuf::from(tzdir));
        }
    }
    for sys in [
        "/usr/share/zoneinfo",
        "/usr/lib/zoneinfo",
        "/etc/zoneinfo",
        "/usr/share/lib/zoneinfo",
    ] {
        let path = Path::new(sys);
        if path.is_dir() {
            roots.push(path.to_path_buf());
        }
    }
    roots
}

#[derive(Debug, Clone, Copy)]
struct TzifHeader {
    version: u8,
    isutcnt: usize,
    isstdcnt: usize,
    leapcnt: usize,
    timecnt: usize,
    typecnt: usize,
    charcnt: usize,
}

fn read_be_u32(slice: &[u8]) -> Option<u32> {
    let bytes: [u8; 4] = slice.try_into().ok()?;
    Some(u32::from_be_bytes(bytes))
}

fn read_be_i32(slice: &[u8]) -> Option<i32> {
    let bytes: [u8; 4] = slice.try_into().ok()?;
    Some(i32::from_be_bytes(bytes))
}

fn read_be_i64(slice: &[u8]) -> Option<i64> {
    let bytes: [u8; 8] = slice.try_into().ok()?;
    Some(i64::from_be_bytes(bytes))
}

fn parse_header(bytes: &[u8], offset: usize) -> Option<TzifHeader> {
    let header = bytes.get(offset..offset + 44)?;
    if &header[0..4] != b"TZif" {
        return None;
    }
    Some(TzifHeader {
        version: header[4],
        isutcnt: read_be_u32(&header[20..24])? as usize,
        isstdcnt: read_be_u32(&header[24..28])? as usize,
        leapcnt: read_be_u32(&header[28..32])? as usize,
        timecnt: read_be_u32(&header[32..36])? as usize,
        typecnt: read_be_u32(&header[36..40])? as usize,
        charcnt: read_be_u32(&header[40..44])? as usize,
    })
}

/// Parse an RFC 8536 TZif byte buffer.
pub(crate) fn parse_tzif(name: &str, bytes: &[u8]) -> Option<IanaTimeZone> {
    let h1 = parse_header(bytes, 0)?;
    if h1.typecnt == 0 {
        return None;
    }

    let (header, data_start, time_size, leap_size) = if h1.version >= b'2' {
        let v1_len = h1
            .timecnt
            .checked_mul(5)?
            .checked_add(h1.typecnt.checked_mul(6)?)?
            .checked_add(h1.charcnt)?
            .checked_add(h1.leapcnt.checked_mul(8)?)?
            .checked_add(h1.isstdcnt)?
            .checked_add(h1.isutcnt)?;
        let h2_offset = 44usize.checked_add(v1_len)?;
        let h2 = parse_header(bytes, h2_offset)?;
        if h2.typecnt == 0 {
            return None;
        }
        (h2, h2_offset + 44, 8usize, 12usize)
    } else {
        (h1, 44usize, 4usize, 8usize)
    };

    let times_len = header.timecnt.checked_mul(time_size)?;
    let types_idx_offset = data_start.checked_add(times_len)?;
    let ttinfo_offset = types_idx_offset.checked_add(header.timecnt)?;
    let char_offset = ttinfo_offset.checked_add(header.typecnt.checked_mul(6)?)?;
    let leap_offset = char_offset.checked_add(header.charcnt)?;
    let std_offset = leap_offset.checked_add(header.leapcnt.checked_mul(leap_size)?)?;
    let ut_offset = std_offset.checked_add(header.isstdcnt)?;
    let footer_start = ut_offset.checked_add(header.isutcnt)?;

    if footer_start > bytes.len() {
        return None;
    }

    let mut type_offsets = Vec::with_capacity(header.typecnt);
    for idx in 0..header.typecnt {
        let pos = ttinfo_offset + idx * 6;
        let utoff = read_be_i32(bytes.get(pos..pos + 4)?)?;
        type_offsets.push(utoff);
    }
    let default_offset = *type_offsets.first()?;

    let mut transitions = Vec::with_capacity(header.timecnt);
    for idx in 0..header.timecnt {
        let pos = data_start + idx * time_size;
        let utc_seconds = if time_size == 8 {
            read_be_i64(bytes.get(pos..pos + 8)?)?
        } else {
            i64::from(read_be_i32(bytes.get(pos..pos + 4)?)?)
        };
        let type_idx = *bytes.get(types_idx_offset + idx)? as usize;
        let offset_seconds = *type_offsets.get(type_idx)?;
        transitions.push(Transition {
            utc_seconds,
            offset_seconds,
        });
    }

    let mut posix_tail = None;
    if h1.version >= b'2' {
        if let Some(tail) = bytes.get(footer_start..) {
            if let Ok(text) = std::str::from_utf8(tail) {
                let trimmed = text.trim();
                if !trimmed.is_empty() {
                    posix_tail = parse_posix_tz(trimmed);
                }
            }
        }
    }

    Some(IanaTimeZone {
        name: name.to_string(),
        fixed_offset: None,
        transitions,
        default_offset,
        posix_tail,
    })
}

fn consume_tz_abbr(input: &str) -> Option<&str> {
    let bytes = input.as_bytes();
    if bytes.is_empty() {
        return None;
    }
    if bytes[0] == b'<' {
        let end = input.find('>')?;
        return input.get(end + 1..);
    }
    let mut len = 0usize;
    while let Some(&b) = bytes.get(len) {
        if b.is_ascii_alphabetic() {
            len += 1;
        } else {
            break;
        }
    }
    if len == 0 { None } else { input.get(len..) }
}

fn parse_posix_offset(input: &str) -> Option<(i32, &str)> {
    let mut sign = -1i32; // POSIX EST5 means UTC-5
    let mut rest = input;
    if let Some(stripped) = rest.strip_prefix('+') {
        sign = -1;
        rest = stripped;
    } else if let Some(stripped) = rest.strip_prefix('-') {
        sign = 1;
        rest = stripped;
    }
    let (secs, tail) = parse_hms_duration(rest, false)?;
    Some((sign * secs, tail))
}

fn parse_hms_duration(input: &str, allow_sign: bool) -> Option<(i32, &str)> {
    let mut sign = 1i32;
    let mut rest = input;
    if allow_sign {
        if let Some(stripped) = rest.strip_prefix('+') {
            rest = stripped;
        } else if let Some(stripped) = rest.strip_prefix('-') {
            sign = -1;
            rest = stripped;
        }
    }
    let (h, after_h) = read_uint(rest)?;
    let (m, after_m) = if let Some(r) = after_h.strip_prefix(':') {
        read_uint(r)?
    } else {
        (0, after_h)
    };
    let (s, tail) = if let Some(r) = after_m.strip_prefix(':') {
        read_uint(r)?
    } else {
        (0, after_m)
    };
    let total = h
        .checked_mul(3600)?
        .checked_add(m.checked_mul(60)?)?
        .checked_add(s)?;
    Some((sign * i32::try_from(total).ok()?, tail))
}

fn read_uint(input: &str) -> Option<(u32, &str)> {
    let mut len = 0usize;
    for b in input.bytes() {
        if b.is_ascii_digit() {
            len += 1;
        } else {
            break;
        }
    }
    if len == 0 {
        return None;
    }
    let val: u32 = input[..len].parse().ok()?;
    Some((val, &input[len..]))
}

fn parse_dst_spec(input: &str) -> Option<(DstTransitionSpec, &str)> {
    let (date, rest) = if let Some(r) = input.strip_prefix('M') {
        let (month, r) = read_uint(r)?;
        let r = r.strip_prefix('.')?;
        let (week, r) = read_uint(r)?;
        let r = r.strip_prefix('.')?;
        let (weekday, r) = read_uint(r)?;
        if !(1..=12).contains(&month) || !(1..=5).contains(&week) || weekday > 6 {
            return None;
        }
        (
            DstDateRule::MonthWeekDay {
                month,
                week,
                weekday,
            },
            r,
        )
    } else if let Some(r) = input.strip_prefix('J') {
        let (day, r) = read_uint(r)?;
        if !(1..=365).contains(&day) {
            return None;
        }
        (DstDateRule::JulianNoLeap(day), r)
    } else {
        let (day, r) = read_uint(input)?;
        if day > 365 {
            return None;
        }
        (DstDateRule::JulianZero(day), r)
    };
    let (time_seconds, tail) = if let Some(r) = rest.strip_prefix('/') {
        let (secs, tail) = parse_hms_duration(r, true)?;
        (i64::from(secs), tail)
    } else {
        (2 * 3600, rest)
    };
    Some((DstTransitionSpec { date, time_seconds }, tail))
}

/// Parse a POSIX TZ specification string like `EST5EDT,M3.2.0,M11.1.0`.
pub(crate) fn parse_posix_tz(spec: &str) -> Option<PosixTzRule> {
    let rest = consume_tz_abbr(spec)?;
    let (std_offset, rest) = parse_posix_offset(rest)?;
    if rest.is_empty() {
        return Some(PosixTzRule {
            std_offset,
            dst: None,
        });
    }
    let rest = consume_tz_abbr(rest)?;
    let (dst_offset, rest) = if rest.starts_with(',') {
        (std_offset + 3600, rest)
    } else {
        parse_posix_offset(rest)?
    };
    let rest = rest.strip_prefix(',')?;
    let (start, rest) = parse_dst_spec(rest)?;
    let rest = rest.strip_prefix(',')?;
    let (end, tail) = parse_dst_spec(rest)?;
    if !tail.is_empty() {
        return None;
    }
    Some(PosixTzRule {
        std_offset,
        dst: Some(PosixDst {
            dst_offset,
            start,
            end,
        }),
    })
}

fn is_leap_year(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0)
}

fn days_in_month(year: i32, month: u32) -> u32 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if is_leap_year(year) {
                29
            } else {
                28
            }
        }
        _ => 30,
    }
}

fn rule_date_in_year(year: i32, rule: &DstDateRule) -> Option<NaiveDate> {
    match *rule {
        DstDateRule::MonthWeekDay {
            month,
            week,
            weekday,
        } => {
            let first = NaiveDate::from_ymd_opt(year, month, 1)?;
            let first_sun0 = first.weekday().num_days_from_sunday();
            let offset = (7 + weekday - first_sun0) % 7;
            let mut day = 1 + offset + (week - 1) * 7;
            let dim = days_in_month(year, month);
            while day > dim {
                day -= 7;
            }
            NaiveDate::from_ymd_opt(year, month, day)
        }
        DstDateRule::JulianNoLeap(j) => {
            // J1..J365 skips Feb 29 even in leap years.
            let adj = if is_leap_year(year) && j > 59 {
                j + 1
            } else {
                j
            };
            NaiveDate::from_yo_opt(year, adj)
        }
        DstDateRule::JulianZero(n) => NaiveDate::from_yo_opt(year, n + 1),
    }
}

fn eval_posix_offset(rule: &PosixTzRule, utc: DateTime<Utc>) -> i32 {
    let Some(dst) = &rule.dst else {
        return rule.std_offset;
    };
    let year = utc.year();
    let Some(start_date) = rule_date_in_year(year, &dst.start.date) else {
        return rule.std_offset;
    };
    let Some(end_date) = rule_date_in_year(year, &dst.end.date) else {
        return rule.std_offset;
    };
    let midnight = NaiveTime::from_hms_opt(0, 0, 0).expect("midnight");
    let start_utc = NaiveDateTime::new(start_date, midnight)
        .and_utc()
        .timestamp()
        + dst.start.time_seconds
        - i64::from(rule.std_offset);
    let end_utc = NaiveDateTime::new(end_date, midnight).and_utc().timestamp()
        + dst.end.time_seconds
        - i64::from(dst.dst_offset);
    let ts = utc.timestamp();
    let in_dst = if start_utc < end_utc {
        ts >= start_utc && ts < end_utc
    } else {
        ts >= start_utc || ts < end_utc
    };
    if in_dst {
        dst.dst_offset
    } else {
        rule.std_offset
    }
}

impl IanaTimeZone {
    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn offset_seconds_at(&self, utc: DateTime<Utc>) -> i32 {
        if let Some(fixed) = self.fixed_offset {
            return fixed;
        }
        let ts = utc.timestamp();
        if let Some(last) = self.transitions.last() {
            if ts >= last.utc_seconds {
                if let Some(rule) = &self.posix_tail {
                    return eval_posix_offset(rule, utc);
                }
                return last.offset_seconds;
            }
        }
        let idx = self
            .transitions
            .partition_point(|entry| entry.utc_seconds <= ts);
        if idx == 0 {
            self.default_offset
        } else {
            self.transitions[idx - 1].offset_seconds
        }
    }

    pub fn to_local(&self, utc: DateTime<Utc>) -> DateTime<FixedOffset> {
        let secs = self.offset_seconds_at(utc);
        let offset = FixedOffset::east_opt(secs)
            .unwrap_or_else(|| FixedOffset::east_opt(0).expect("utc offset"));
        utc.with_timezone(&offset)
    }

    pub fn local_hour_minute(&self, unix_seconds: i64) -> (u32, u32) {
        let utc = DateTime::<Utc>::from_timestamp(unix_seconds, 0)
            .unwrap_or_else(|| DateTime::<Utc>::from_timestamp(0, 0).expect("unix epoch"));
        let local = self.to_local(utc);
        (local.hour(), local.minute())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn validates_and_resolves_builtin_offsets() {
        assert!(load_iana_timezone("").is_err());
        assert!(load_iana_timezone("/etc/passwd").is_err());
        assert!(load_iana_timezone("../UTC").is_err());
        assert!(load_iana_timezone("Invalid/Nonexistent_Zone").is_err());

        let utc = load_iana_timezone("UTC").unwrap();
        let instant = Utc.with_ymd_and_hms(2026, 6, 15, 12, 0, 0).unwrap();
        assert_eq!(utc.offset_seconds_at(instant), 0);

        let plus_eight = load_iana_timezone("Etc/GMT-8").unwrap();
        assert_eq!(plus_eight.local_hour_minute(1_700_000_000), (6, 13));
        assert_eq!(plus_eight.offset_seconds_at(instant), 8 * 3600);
        assert_eq!(plus_eight.to_local(instant).format("%H").to_string(), "20");

        let minus_five = load_iana_timezone("Etc/GMT+5").unwrap();
        assert_eq!(minus_five.offset_seconds_at(instant), -5 * 3600);
    }

    #[test]
    fn posix_tz_rule_handles_us_and_southern_dst() {
        let us_east = parse_posix_tz("EST5EDT,M3.2.0,M11.1.0").unwrap();
        // 2026-03-08 06:59:59Z is 01:59:59 EST (-5h); 07:00:00Z springs to 03:00:00 EDT (-4h).
        let before_spring = Utc.with_ymd_and_hms(2026, 3, 8, 6, 59, 59).unwrap();
        let at_spring = Utc.with_ymd_and_hms(2026, 3, 8, 7, 0, 0).unwrap();
        assert_eq!(eval_posix_offset(&us_east, before_spring), -5 * 3600);
        assert_eq!(eval_posix_offset(&us_east, at_spring), -4 * 3600);

        // 2026-11-01 05:59:59Z is 01:59:59 EDT (-4h); 06:00:00Z falls back to 01:00:00 EST (-5h).
        let before_fall = Utc.with_ymd_and_hms(2026, 11, 1, 5, 59, 59).unwrap();
        let at_fall = Utc.with_ymd_and_hms(2026, 11, 1, 6, 0, 0).unwrap();
        assert_eq!(eval_posix_offset(&us_east, before_fall), -4 * 3600);
        assert_eq!(eval_posix_offset(&us_east, at_fall), -5 * 3600);

        // Southern hemisphere: AEST-10AEDT,M10.1.0,M4.1.0/3
        let sydney = parse_posix_tz("AEST-10AEDT,M10.1.0,M4.1.0/3").unwrap();
        let winter = Utc.with_ymd_and_hms(2026, 7, 1, 0, 0, 0).unwrap();
        let summer = Utc.with_ymd_and_hms(2026, 1, 15, 0, 0, 0).unwrap();
        assert_eq!(eval_posix_offset(&sydney, winter), 10 * 3600);
        assert_eq!(eval_posix_offset(&sydney, summer), 11 * 3600);
    }

    #[test]
    fn parses_synthetic_tzif_v2_with_tail() {
        let mut buf = Vec::new();
        // v1 minimal header (typecnt=1, everything else 0)
        buf.extend_from_slice(b"TZif2");
        buf.extend_from_slice(&[0u8; 15]);
        buf.extend_from_slice(&0u32.to_be_bytes());
        buf.extend_from_slice(&0u32.to_be_bytes());
        buf.extend_from_slice(&0u32.to_be_bytes());
        buf.extend_from_slice(&0u32.to_be_bytes());
        buf.extend_from_slice(&1u32.to_be_bytes());
        buf.extend_from_slice(&4u32.to_be_bytes());
        // v1 body: 1 ttinfo (6 bytes) + 4 abbr bytes
        buf.extend_from_slice(&(-18000i32).to_be_bytes());
        buf.extend_from_slice(&[0, 0]);
        buf.extend_from_slice(b"EST\0");

        // v2 header (timecnt=1, typecnt=2, charcnt=8)
        buf.extend_from_slice(b"TZif2");
        buf.extend_from_slice(&[0u8; 15]);
        buf.extend_from_slice(&0u32.to_be_bytes());
        buf.extend_from_slice(&0u32.to_be_bytes());
        buf.extend_from_slice(&0u32.to_be_bytes());
        buf.extend_from_slice(&1u32.to_be_bytes());
        buf.extend_from_slice(&2u32.to_be_bytes());
        buf.extend_from_slice(&8u32.to_be_bytes());
        // 1 transition at t = 1_000_000_000i64 -> type 1 (-4h)
        buf.extend_from_slice(&1_000_000_000i64.to_be_bytes());
        buf.push(1);
        // type 0: -5h, type 1: -4h
        buf.extend_from_slice(&(-18000i32).to_be_bytes());
        buf.extend_from_slice(&[0, 0]);
        buf.extend_from_slice(&(-14400i32).to_be_bytes());
        buf.extend_from_slice(&[1, 0]);
        buf.extend_from_slice(b"EST\0EDT\0");
        buf.extend_from_slice(b"\nEST5EDT,M3.2.0,M11.1.0\n");

        let parsed = parse_tzif("Test/Zone", &buf).expect("tzif");
        let early = Utc.with_ymd_and_hms(1990, 1, 1, 0, 0, 0).unwrap();
        let summer_2026 = Utc.with_ymd_and_hms(2026, 7, 1, 0, 0, 0).unwrap();
        let winter_2026 = Utc.with_ymd_and_hms(2026, 12, 1, 0, 0, 0).unwrap();
        assert_eq!(parsed.offset_seconds_at(early), -18000);
        assert_eq!(parsed.offset_seconds_at(summer_2026), -14400);
        assert_eq!(parsed.offset_seconds_at(winter_2026), -18000);
    }
}
