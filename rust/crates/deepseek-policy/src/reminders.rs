//! The reminders store and its two dispatch branches, mirroring
//! `deepseek_infra/infra/data/reminders.py` plus the
//! `create_reminder_tool` / `list_reminders_tool` wrappers in `tools.py`.
//!
//! Storage is one JSON array at `<root>/.reminders/reminders.json`, written as
//! Python's `json.dumps(..., ensure_ascii=False, indent=2)` with the record's
//! key order preserved (see [`crate::python_json::OrderedJson`]). Every write goes
//! through [`crate::mutation_gate::mutation_scope`], so a reminder write
//! participates in the backup-consistency protocol rather than being a plain file
//! write.
//!
//! # Two quirks reproduced deliberately
//!
//! - The temp file is `REMINDERS_FILE.with_suffix(".tmp")`, which **replaces** the
//!   suffix: for `reminders.json` that is `reminders.tmp`, not
//!   `reminders.json.tmp`. Two writers racing the same directory therefore collide
//!   on one temp name. That is the oracle's behaviour and is not "fixed" here.
//! - Reads are silent: a missing file, an unreadable file, malformed JSON or a
//!   wrong top-level type all degrade to an empty list, and non-dict entries are
//!   dropped. Corruption becomes "no reminders", never an error.
//!
//! # Date handling
//!
//! [`parse_due_at`] reproduces the subset of `datetime.fromisoformat` this module
//! actually meets, plus Python's `isoformat()` rendering. The accepted forms and
//! the rejections are pinned by measurement in
//! `tasks/native-runtime/reminders_parity_probe.py`; anything outside them raises
//! the same `Reminder dueAt must be an ISO datetime` error rather than being
//! guessed at.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde_json::{Map, Value, json};

use crate::app_error::AppError;
use crate::mutation_gate::mutation_scope;
use crate::python_json::OrderedJson;

/// `MAX_REMINDERS`. A tail slice after sorting by `dueAt`, so it keeps the
/// latest-due entries.
pub const MAX_REMINDERS: usize = 200;

/// The record's key order, as the oracle's dict literal writes it.
pub const RECORD_KEYS: [&str; 6] = ["id", "title", "content", "dueAt", "createdAt", "notified"];
/// The same order plus the optional `notifiedAt`, appended when present.
pub const RECORD_KEYS_NOTIFIED: [&str; 7] = [
    "id",
    "title",
    "content",
    "dueAt",
    "createdAt",
    "notified",
    "notifiedAt",
];

/// Mirrors `_LOCK`: serializes threads for this process. The cross-process part is
/// the mutation gate.
static STORE_LOCK: Mutex<()> = Mutex::new(());

// --- paths -----------------------------------------------------------------------

pub fn reminders_dir(root: &Path) -> PathBuf {
    root.join(".reminders")
}

pub fn reminders_file(root: &Path) -> PathBuf {
    reminders_dir(root).join("reminders.json")
}

/// `REMINDERS_FILE.with_suffix(".tmp")` — note this **replaces** `.json`.
fn temp_file(root: &Path) -> PathBuf {
    reminders_dir(root).join("reminders.tmp")
}

// --- entropy ---------------------------------------------------------------------

/// The two non-deterministic inputs, injected so the store is testable and the
/// parity probe can pin them.
pub trait Entropy {
    /// Mirrors `secrets.token_hex(8)` — 16 lowercase hex characters.
    fn new_id(&self) -> Result<String, AppError>;
    /// Mirrors `int(time.time() * 1000)`.
    fn now_millis(&self) -> i64;
}

/// The production source: an OS CSPRNG and the wall clock.
pub struct SystemEntropy;

impl Entropy for SystemEntropy {
    fn new_id(&self) -> Result<String, AppError> {
        let mut bytes = [0u8; 8];
        os_random(&mut bytes)?;
        Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
    }

    fn now_millis(&self) -> i64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_millis() as i64)
            .unwrap_or(0)
    }
}

/// Fill `buffer` from the OS CSPRNG.
///
/// `secrets.token_hex` is explicitly a *secure* source, so this does not fall back
/// to a seeded but non-cryptographic generator: a reminder id is handed to the
/// model in tool output, and making it guessable would be a real regression.
#[cfg(unix)]
fn os_random(buffer: &mut [u8]) -> Result<(), AppError> {
    use std::io::Read;
    let mut file = std::fs::File::open("/dev/urandom")
        .map_err(|error| AppError::invalid_payload(format!("no secure random source: {error}")))?;
    file.read_exact(buffer)
        .map_err(|error| AppError::invalid_payload(format!("no secure random source: {error}")))
}

#[cfg(windows)]
fn os_random(buffer: &mut [u8]) -> Result<(), AppError> {
    use std::ffi::c_void;

    #[link(name = "bcrypt")]
    unsafe extern "C" {
        fn BCryptGenRandom(algorithm: *mut c_void, buffer: *mut u8, length: u32, flags: u32)
        -> i32;
    }

    // BCRYPT_USE_SYSTEM_PREFERRED_RNG
    const USE_SYSTEM_PREFERRED_RNG: u32 = 0x0000_0002;
    let status = unsafe {
        BCryptGenRandom(
            std::ptr::null_mut(),
            buffer.as_mut_ptr(),
            buffer.len() as u32,
            USE_SYSTEM_PREFERRED_RNG,
        )
    };
    if status != 0 {
        return Err(AppError::invalid_payload(format!(
            "no secure random source: BCryptGenRandom returned {status}"
        )));
    }
    Ok(())
}

// --- the store -------------------------------------------------------------------

/// Mirrors `_read_reminders`: tolerant to the point of silence.
pub fn load_reminders(root: &Path) -> Vec<Value> {
    let _guard = STORE_LOCK.lock();
    read_unlocked(root)
}

fn read_unlocked(root: &Path) -> Vec<Value> {
    let Ok(raw) = std::fs::read_to_string(reminders_file(root)) else {
        return Vec::new();
    };
    let Ok(value) = serde_json::from_str::<Value>(&raw) else {
        return Vec::new();
    };
    match value {
        Value::Array(items) => items.into_iter().filter(Value::is_object).collect(),
        _ => Vec::new(),
    }
}

/// Mirrors `_write_reminders`, including the gate and the replaced-suffix temp name.
pub fn save_reminders(root: &Path, reminders: &[Value]) -> Result<(), AppError> {
    let _scope = mutation_scope(None, root).map_err(|error| AppError {
        message: error.message,
        code: crate::app_error::codes::INVALID_REQUEST,
        status: error.status.unwrap_or(500),
    })?;

    let directory = reminders_dir(root);
    std::fs::create_dir_all(&directory)
        .map_err(|error| AppError::invalid_payload(error.to_string()))?;

    let ordered: Vec<OrderedJson> = reminders
        .iter()
        .map(|item| OrderedJson::from_value_with_order(item, &RECORD_KEYS_NOTIFIED))
        .collect();
    let rendered = OrderedJson::List(ordered).render_indent_2();

    let temporary = temp_file(root);
    std::fs::write(&temporary, rendered.as_bytes())
        .map_err(|error| AppError::invalid_payload(error.to_string()))?;
    std::fs::rename(&temporary, reminders_file(root))
        .map_err(|error| AppError::invalid_payload(error.to_string()))?;
    Ok(())
}

// --- the branches ----------------------------------------------------------------

/// Mirrors the `create_reminder` branch: stringify, truncate, parse, then append.
pub fn create_reminder(
    arguments: &Map<String, Value>,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    // `str(payload.get("title") or "提醒").strip()[:120] or "提醒"`
    let raw_title = python_str_or(arguments.get("title"), "提醒");
    let title: String = raw_title.trim().chars().take(120).collect();
    let title = if title.is_empty() {
        "提醒".to_string()
    } else {
        title
    };
    let content: String = python_str_or(arguments.get("content"), "")
        .trim()
        .chars()
        .take(2000)
        .collect();
    // `payload.get("dueAt") or payload.get("due_at")`
    let due_source = match arguments.get("dueAt") {
        Some(Value::Null) | None => arguments.get("due_at"),
        other => other,
    };
    let due_at = parse_due_at(due_source)?;

    let reminder = json!({
        "id": entropy.new_id()?,
        "title": title,
        "content": content,
        "dueAt": due_at,
        "createdAt": entropy.now_millis(),
        "notified": false,
    });

    {
        let _guard = STORE_LOCK.lock();
        // Already-notified entries are dropped on every create.
        let mut reminders: Vec<Value> = read_unlocked(root)
            .into_iter()
            .filter(|item| {
                !item
                    .get("notified")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
            })
            .collect();
        reminders.push(reminder.clone());
        // Sorted ascending by the `dueAt` *string*, then the last MAX_REMINDERS.
        reminders.sort_by_key(|item| match item.get("dueAt").and_then(Value::as_str) {
            Some(text) => text.to_string(),
            None => String::new(),
        });
        if reminders.len() > MAX_REMINDERS {
            reminders = reminders.split_off(reminders.len() - MAX_REMINDERS);
        }
        save_reminders(root, &reminders)?;
    }
    Ok(reminder)
}

/// Mirrors `list_reminders_tool`.
///
/// Note the deliberate asymmetry: `count` is the **untruncated** filtered length
/// while `reminders` is capped at 50, so the two legitimately disagree.
pub fn list_reminders(arguments: &Map<String, Value>, root: &Path) -> Value {
    let requested = python_str_or(arguments.get("status"), "active");
    let normalized = requested.trim().to_lowercase();
    let normalized = if matches!(normalized.as_str(), "active" | "notified" | "all") {
        normalized
    } else {
        "active".to_string()
    };

    let reminders = load_reminders(root);
    let filtered: Vec<&Value> = reminders
        .iter()
        .filter(|item| {
            let notified = item
                .get("notified")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            match normalized.as_str() {
                "active" => !notified,
                "notified" => notified,
                _ => true,
            }
        })
        .collect();

    let count = filtered.len();
    let reminders: Vec<Value> = filtered.into_iter().take(50).cloned().collect();
    json!({"status": normalized, "reminders": reminders, "count": count})
}

/// Mirrors `delete_reminder`.
pub fn delete_reminder(reminder_id: &str, root: &Path) -> Result<i64, AppError> {
    let reminder_id = reminder_id.trim();
    if reminder_id.is_empty() {
        return Ok(0);
    }
    let _guard = STORE_LOCK.lock();
    let reminders = read_unlocked(root);
    let next: Vec<Value> = reminders
        .iter()
        .filter(|item| item.get("id").and_then(Value::as_str) != Some(reminder_id))
        .cloned()
        .collect();
    if next.len() != reminders.len() {
        save_reminders(root, &next)?;
        return Ok(1);
    }
    Ok(0)
}

/// Mirrors `due_reminders`, taking `now` explicitly instead of defaulting to the
/// wall clock, so the caller decides.
///
/// Returns the newly-due entries and marks them notified, rewriting the store only
/// when something became due.
pub fn due_reminders(root: &Path, now_millis: i64) -> Result<Vec<Value>, AppError> {
    let _guard = STORE_LOCK.lock();
    let mut reminders = read_unlocked(root);
    let mut due: Vec<Value> = Vec::new();
    for item in reminders.iter_mut() {
        if item
            .get("notified")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        // The oracle re-parses the stored value; a value it cannot parse is
        // skipped rather than failing the whole call.
        let Ok(due_at) = parse_due_at(item.get("dueAt")) else {
            continue;
        };
        let Ok(due_epoch) = epoch_millis(&due_at) else {
            continue;
        };
        if due_epoch <= now_millis {
            if let Some(object) = item.as_object_mut() {
                object.insert("notified".to_string(), Value::Bool(true));
                object.insert("notifiedAt".to_string(), json!(now_millis));
            }
            due.push(item.clone());
        }
    }
    if !due.is_empty() {
        save_reminders(root, &reminders)?;
    }
    Ok(due)
}

// --- date handling ---------------------------------------------------------------

/// Mirrors `parse_due_at`: normalise and render UTC, or raise.
pub fn parse_due_at(value: Option<&Value>) -> Result<String, AppError> {
    let text = python_str_or(value, "");
    let text = text.trim();
    if text.is_empty() {
        return Err(AppError::invalid_payload("Reminder dueAt is required"));
    }
    // Only an uppercase trailing `Z` is rewritten; a lowercase one is left to the
    // parser, which rejects it.
    let normalized = match text.strip_suffix('Z') {
        Some(head) => format!("{head}+00:00"),
        None => text.to_string(),
    };
    let parsed = parse_iso(&normalized)
        .ok_or_else(|| AppError::invalid_payload("Reminder dueAt must be an ISO datetime"))?;
    // A naive value is taken as UTC.
    Ok(parsed.to_isoformat_utc())
}

/// A civil timestamp with an optional offset, i.e. the shape `fromisoformat`
/// produces.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IsoDateTime {
    year: i32,
    month: u32,
    day: u32,
    hour: u32,
    minute: u32,
    second: u32,
    micros: u32,
    /// Offset from UTC in seconds.
    offset_seconds: i32,
}

impl IsoDateTime {
    /// Python's `datetime.astimezone(timezone.utc).isoformat()`.
    ///
    /// The microseconds are omitted when zero, which is why `10:30:00.123456Z`
    /// keeps six digits but `10:30:00Z` gains none.
    pub fn to_isoformat_utc(self) -> String {
        let (year, month, day, hour, minute, second) = self.to_utc_civil();
        let base = format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}");
        if self.micros == 0 {
            format!("{base}+00:00")
        } else {
            format!("{base}.{:06}+00:00", self.micros)
        }
    }

    /// Days-from-civil (Howard Hinnant) so offsets can be applied without a
    /// calendar library.
    fn to_utc_civil(self) -> (i32, u32, u32, u32, u32, u32) {
        let days = days_from_civil(self.year, self.month, self.day);
        let seconds =
            days * 86_400 + self.hour as i64 * 3600 + self.minute as i64 * 60 + self.second as i64
                - self.offset_seconds as i64;
        let (days, remainder) = (seconds.div_euclid(86_400), seconds.rem_euclid(86_400));
        let (year, month, day) = civil_from_days(days);
        (
            year,
            month,
            day,
            (remainder / 3600) as u32,
            ((remainder % 3600) / 60) as u32,
            (remainder % 60) as u32,
        )
    }
}

/// The epoch milliseconds of an already-normalised ISO string, for `due_reminders`.
fn epoch_millis(iso: &str) -> Result<i64, AppError> {
    let parsed = parse_iso(iso)
        .ok_or_else(|| AppError::invalid_payload("Reminder dueAt must be an ISO datetime"))?;
    let days = days_from_civil(parsed.year, parsed.month, parsed.day);
    Ok(days * 86_400_000
        + parsed.hour as i64 * 3_600_000
        + parsed.minute as i64 * 60_000
        + parsed.second as i64 * 1_000
        + (parsed.micros / 1_000) as i64
        - parsed.offset_seconds as i64 * 1_000)
}

/// Parse the ISO forms this module meets.
///
/// Accepted, as measured: `YYYY-MM-DD`, `YYYYMMDD`, `YYYY-Www-D`,
/// `YYYY-MM-DDTHH`, `HH:MM`, `HH:MM:SS`, `HH:MM:SS.ffffff`, the compact time
/// `HHMMSS`, a `T`, `t` or space separator, and a trailing `Z`, `+HH`, `+HH:MM` or
/// `+HHMM` offset. Anything else is `None`, which the caller turns into the
/// oracle's "must be an ISO datetime" error.
fn parse_iso(text: &str) -> Option<IsoDateTime> {
    let (date_part, rest) = split_date(text)?;
    let (year, month, day) = parse_date(date_part)?;
    if !is_valid_date(year, month, day) {
        return None;
    }

    let (time_part, offset_seconds) = match rest {
        None => ("", 0),
        Some(rest) => split_offset(rest)?,
    };

    let (hour, minute, second, micros) = parse_time(time_part)?;
    Some(IsoDateTime {
        year,
        month,
        day,
        hour,
        minute,
        second,
        micros,
        offset_seconds,
    })
}

/// Split off the date, leaving the time-and-offset rest.
fn split_date(text: &str) -> Option<(&str, Option<&str>)> {
    // A week date has the form `YYYY-Www-D`, which contains `-` before the `W`.
    if let Some(week_index) = text.find(['W', 'w']) {
        if week_index >= 5 && text.as_bytes().get(week_index - 1) == Some(&b'-') {
            let end = text[week_index..]
                .find(['T', 't', ' '])
                .map(|offset| week_index + offset)
                .unwrap_or(text.len());
            return Some((&text[..end], (end < text.len()).then(|| &text[end..])));
        }
    }
    match text.find(['T', 't', ' ']) {
        Some(index) => Some((&text[..index], Some(&text[index..]))),
        None => Some((text, None)),
    }
}

fn parse_date(text: &str) -> Option<(i32, u32, u32)> {
    let bytes = text.as_bytes();
    // `YYYY-Www-D`
    if bytes.len() == 10 && (bytes[5] == b'W' || bytes[5] == b'w') && bytes[4] == b'-' {
        let year: i32 = text[..4].parse().ok()?;
        let week: u32 = text[6..8].parse().ok()?;
        let weekday: u32 = text[9..].parse().ok()?;
        return iso_week_to_civil(year, week, weekday);
    }
    // `YYYYMMDD`
    if bytes.len() == 8 && bytes.iter().all(u8::is_ascii_digit) {
        return Some((
            text[..4].parse().ok()?,
            text[4..6].parse().ok()?,
            text[6..].parse().ok()?,
        ));
    }
    // `YYYY-MM-DD`
    if bytes.len() == 10 && bytes[4] == b'-' && bytes[7] == b'-' {
        return Some((
            text[..4].parse().ok()?,
            text[5..7].parse().ok()?,
            text[8..].parse().ok()?,
        ));
    }
    None
}

/// Parse the time, dropping a leading separator.
fn parse_time(text: &str) -> Option<(u32, u32, u32, u32)> {
    if text.is_empty() {
        return Some((0, 0, 0, 0));
    }
    let body = text.strip_prefix(['T', 't', ' ']).unwrap_or(text);
    if body.is_empty() {
        return Some((0, 0, 0, 0));
    }
    let (clock, fraction) = match body.split_once('.') {
        Some((clock, fraction)) => (clock, Some(fraction)),
        None => (body, None),
    };
    let (hour, minute, second) = if let Some((hours, minutes)) = clock.split_once(':') {
        match minutes.split_once(':') {
            Some((minutes, seconds)) => (
                hours.parse().ok()?,
                minutes.parse().ok()?,
                seconds.parse().ok()?,
            ),
            None => (hours.parse().ok()?, minutes.parse().ok()?, 0),
        }
    } else if clock.len() == 6 && clock.bytes().all(|byte| byte.is_ascii_digit()) {
        (
            clock[..2].parse().ok()?,
            clock[2..4].parse().ok()?,
            clock[4..].parse().ok()?,
        )
    } else if clock.len() <= 2 && clock.bytes().all(|byte| byte.is_ascii_digit()) {
        (clock.parse().ok()?, 0, 0)
    } else {
        return None;
    };
    if hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    // A fraction is padded to microseconds, so `.1` becomes `100000`.
    let micros = match fraction {
        None => 0,
        Some(digits) => {
            if digits.is_empty() || digits.len() > 6 || !digits.bytes().all(|b| b.is_ascii_digit())
            {
                return None;
            }
            let mut padded = digits.to_string();
            while padded.len() < 6 {
                padded.push('0');
            }
            padded.parse().ok()?
        }
    };
    Some((hour, minute, second, micros))
}

/// Split a trailing UTC offset off the time, returning both.
fn split_offset(text: &str) -> Option<(&str, i32)> {
    let (clock, offset) = if let Some(position) = text.rfind(['+', '-']) {
        // A `-` in the date has already been consumed, so this is the offset sign;
        // guard against a leading sign being the only one.
        if position == 0 {
            (text, None)
        } else {
            (&text[..position], Some(&text[position..]))
        }
    } else {
        (text, None)
    };
    match offset {
        None => Some((clock, 0)),
        Some(offset) => {
            let sign = if offset.starts_with('-') { -1 } else { 1 };
            let digits = &offset[1..];
            let (hours, minutes) = if let Some((hours, minutes)) = digits.split_once(':') {
                (hours, minutes)
            } else if digits.len() == 4 {
                (&digits[..2], &digits[2..])
            } else if digits.len() <= 2 {
                (digits, "0")
            } else {
                return None;
            };
            let hours: i32 = hours.parse().ok()?;
            let minutes: i32 = minutes.parse().ok()?;
            if minutes > 59 {
                return None;
            }
            Some((clock, sign * (hours * 3600 + minutes * 60)))
        }
    }
}

fn is_leap(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

fn is_valid_date(year: i32, month: u32, day: u32) -> bool {
    if !(1..=12).contains(&month) || day == 0 {
        return false;
    }
    let maximum = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        _ => {
            if is_leap(year) {
                29
            } else {
                28
            }
        }
    };
    day <= maximum
}

/// `YYYY-Www-D` to a civil date. `weekday` is 1 = Monday .. 7 = Sunday.
///
/// The week is validated against **how many ISO weeks the year actually has**, not
/// against the year of the resulting date: `2026-W01-1` is `2025-12-29` (week 1 of
/// 2026 starts in December), while `2026-W53-1` is valid because 2026 has 53 ISO
/// weeks and `2025-W53-1` is not because 2025 has 52. Both were measured against
/// `date.fromisocalendar`; my first version rejected `2026-W01-1` and accepted
/// nothing at week 53, which is the opposite of the oracle on both counts.
fn iso_week_to_civil(year: i32, week: u32, weekday: u32) -> Option<(i32, u32, u32)> {
    if !(1..=7).contains(&weekday) {
        return None;
    }
    let week1_monday = week1_monday_of(year);
    let weeks_in_year = (week1_monday_of(year + 1) - week1_monday) / 7;
    if week < 1 || week as i64 > weeks_in_year {
        return None;
    }
    let target = week1_monday + (week as i64 - 1) * 7 + (weekday as i64 - 1);
    let (result_year, month, day) = civil_from_days(target);
    Some((result_year, month, day))
}

/// The Monday that starts ISO week 1 of `year` — the Monday of the week containing
/// January 4th.
fn week1_monday_of(year: i32) -> i64 {
    let jan4 = days_from_civil(year, 1, 4);
    jan4 - (iso_weekday(jan4) as i64 - 1)
}

/// 1 = Monday .. 7 = Sunday for a day number where 1970-01-01 was a Thursday.
fn iso_weekday(days: i64) -> u32 {
    (days.rem_euclid(7) as u32 + 3) % 7 + 1
}

/// Days since 1970-01-01 for a civil date (Howard Hinnant's algorithm).
fn days_from_civil(year: i32, month: u32, day: u32) -> i64 {
    let year = if month <= 2 { year - 1 } else { year } as i64;
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let month = month as i64;
    let day = day as i64;
    let day_of_year = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

fn civil_from_days(days: i64) -> (i32, u32, u32) {
    let days = days + 719_468;
    let era = if days >= 0 { days } else { days - 146_096 } / 146_097;
    let day_of_era = days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = if month_prime < 10 {
        month_prime + 3
    } else {
        month_prime - 9
    };
    (
        (year + if month <= 2 { 1 } else { 0 }) as i32,
        month as u32,
        day as u32,
    )
}

/// `str(value or fallback)` for the shapes a reminder field can hold.
fn python_str_or(value: Option<&Value>, fallback: &str) -> String {
    match value {
        Some(Value::String(text)) if !text.is_empty() => text.clone(),
        Some(Value::Number(number)) => number.to_string(),
        Some(Value::Bool(true)) => "True".to_string(),
        _ => fallback.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};

    /// Deterministic entropy, mirroring the probe's patched `secrets`/`time`.
    struct FixedEntropy {
        ids: AtomicU64,
    }

    impl FixedEntropy {
        fn new() -> Self {
            Self {
                ids: AtomicU64::new(0),
            }
        }
    }

    impl Entropy for FixedEntropy {
        fn new_id(&self) -> Result<String, AppError> {
            let value = self.ids.fetch_add(1, Ordering::Relaxed) + 1;
            Ok(format!("{value:016x}"))
        }

        fn now_millis(&self) -> i64 {
            1_760_000_000_000
        }
    }

    static COUNTER: AtomicI64 = AtomicI64::new(0);

    fn temp_root(label: &str) -> PathBuf {
        let unique = COUNTER.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "reminders-test-{label}-{}-{unique}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).expect("create temp root");
        root
    }

    // --- parse_due_at, pinned by the probe's corpus ---------------------------

    #[test]
    fn due_at_accepts_the_forms_the_oracle_accepts() {
        let cases = [
            ("2026-09-15T10:30:00Z", "2026-09-15T10:30:00+00:00"),
            ("2026-09-15T10:30:00+08:00", "2026-09-15T02:30:00+00:00"),
            ("2026-09-15T10:30:00-05:00", "2026-09-15T15:30:00+00:00"),
            ("2026-09-15T10:30:00", "2026-09-15T10:30:00+00:00"),
            ("2026-09-15T10:30", "2026-09-15T10:30:00+00:00"),
            ("2026-09-15T10", "2026-09-15T10:00:00+00:00"),
            ("2026-09-15", "2026-09-15T00:00:00+00:00"),
            ("20260915", "2026-09-15T00:00:00+00:00"),
            ("20260915T103000", "2026-09-15T10:30:00+00:00"),
            ("2026-W37-1", "2026-09-07T00:00:00+00:00"),
            ("2026-09-15t10:30:00", "2026-09-15T10:30:00+00:00"),
            ("2026-09-15 10:30:00", "2026-09-15T10:30:00+00:00"),
            ("2026-09-15T10:30:00+0000", "2026-09-15T10:30:00+00:00"),
            ("2026-09-15T10:30:00+08", "2026-09-15T02:30:00+00:00"),
            (
                "2026-09-15T10:30:00.123456",
                "2026-09-15T10:30:00.123456+00:00",
            ),
            ("2026-09-15T10:30:00.5Z", "2026-09-15T10:30:00.500000+00:00"),
            ("2026-09-15T10:30:00.1", "2026-09-15T10:30:00.100000+00:00"),
            ("  2026-09-15T10:30:00Z  ", "2026-09-15T10:30:00+00:00"),
        ];
        for (input, expected) in cases {
            assert_eq!(
                parse_due_at(Some(&json!(input))).unwrap(),
                expected,
                "{input}"
            );
        }
    }

    #[test]
    fn due_at_rejects_what_the_oracle_rejects() {
        for input in [
            json!(""),
            json!(null),
            json!("not a date"),
            // A lowercase `z` is not rewritten and the parser refuses it.
            json!("2026-09-15T10:30:00z"),
            json!("2026-09-15t10:30:00z"),
            json!("2026-09-15T25:00:00"),
            json!("2026-02-30"),
        ] {
            let failure = parse_due_at(Some(&input)).unwrap_err();
            assert_eq!(
                failure.code,
                crate::app_error::codes::INVALID_PAYLOAD,
                "{input}"
            );
        }
        // The two distinct messages.
        assert_eq!(
            parse_due_at(Some(&json!(""))).unwrap_err().message,
            "Reminder dueAt is required"
        );
        assert_eq!(
            parse_due_at(Some(&json!("nope"))).unwrap_err().message,
            "Reminder dueAt must be an ISO datetime"
        );
    }

    /// Measured against `date.fromisocalendar`, which is stricter and looser than
    /// my first guess in different places.
    #[test]
    fn due_at_handles_leap_years_and_the_iso_week_boundary() {
        assert_eq!(
            parse_due_at(Some(&json!("2024-02-29"))).unwrap(),
            "2024-02-29T00:00:00+00:00"
        );
        assert!(parse_due_at(Some(&json!("2023-02-29"))).is_err());
        // Week 1 of 2026 starts in December 2025.
        assert_eq!(
            parse_due_at(Some(&json!("2026-W01-1"))).unwrap(),
            "2025-12-29T00:00:00+00:00"
        );
        // 2026 does have 53 ISO weeks, so W53 is valid...
        assert_eq!(
            parse_due_at(Some(&json!("2026-W53-1"))).unwrap(),
            "2026-12-28T00:00:00+00:00"
        );
        // ...while 2025 has 52.
        assert!(parse_due_at(Some(&json!("2025-W53-1"))).is_err());
        // 2020 is a long year too.
        assert_eq!(
            parse_due_at(Some(&json!("2020-W53-1"))).unwrap(),
            "2020-12-28T00:00:00+00:00"
        );
        assert!(parse_due_at(Some(&json!("2026-W00-1"))).is_err());
        assert!(parse_due_at(Some(&json!("2026-W54-1"))).is_err());
    }

    // --- the store ------------------------------------------------------------

    #[test]
    fn the_store_file_matches_pythons_layout_and_key_order() {
        let root = temp_root("layout");
        let entropy = FixedEntropy::new();
        let mut arguments = Map::new();
        arguments.insert("title".to_string(), json!("Stand up"));
        arguments.insert("content".to_string(), json!("stretch"));
        arguments.insert("dueAt".to_string(), json!("2026-09-15T10:30:00Z"));
        create_reminder(&arguments, &root, &entropy).unwrap();

        let written = std::fs::read_to_string(reminders_file(&root)).unwrap();
        let expected = "[\n  {\n    \"id\": \"0000000000000001\",\n    \"title\": \"Stand up\",\n    \"content\": \"stretch\",\n    \"dueAt\": \"2026-09-15T10:30:00+00:00\",\n    \"createdAt\": 1760000000000,\n    \"notified\": false\n  }\n]";
        assert_eq!(written, expected);
        // No trailing newline, unlike most text files.
        assert!(!written.ends_with('\n'));
        let _ = std::fs::remove_dir_all(&root);
    }

    /// The write goes through the fence, so the generation advances twice per save.
    #[test]
    fn a_create_participates_in_the_backup_generation_protocol() {
        let root = temp_root("fence");
        let entropy = FixedEntropy::new();
        let mut arguments = Map::new();
        arguments.insert("dueAt".to_string(), json!("2026-09-15T10:30:00Z"));
        create_reminder(&arguments, &root, &entropy).unwrap();
        let generation = std::fs::read_to_string(root.join(".workspace-generation")).unwrap();
        assert_eq!(generation, "2");
        assert!(root.join(".workspace-mutation.lock").exists());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn reads_are_silent_about_corruption() {
        let root = temp_root("tolerant");
        let directory = reminders_dir(&root);
        std::fs::create_dir_all(&directory).unwrap();
        let file = reminders_file(&root);

        // Missing, malformed, wrong top-level type, and mixed items all degrade.
        assert!(load_reminders(&root).is_empty());
        std::fs::write(&file, "{not json").unwrap();
        assert!(load_reminders(&root).is_empty());
        std::fs::write(&file, "42").unwrap();
        assert!(load_reminders(&root).is_empty());
        std::fs::write(&file, "{}").unwrap();
        assert!(load_reminders(&root).is_empty());
        std::fs::write(&file, "[]").unwrap();
        assert!(load_reminders(&root).is_empty());
        std::fs::write(&file, "[{\"id\": \"a\"}, \"x\", 7, null, {\"id\": \"b\"}]").unwrap();
        let loaded = load_reminders(&root);
        assert_eq!(loaded.len(), 2);
        assert_eq!(loaded[0]["id"], "a");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn the_temp_file_replaces_the_suffix_like_the_oracle() {
        let root = temp_root("temp-name");
        // `with_suffix(".tmp")` on `reminders.json` gives `reminders.tmp`.
        assert_eq!(temp_file(&root), reminders_dir(&root).join("reminders.tmp"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn create_stringifies_and_truncates_like_the_oracle() {
        let root = temp_root("create");
        let entropy = FixedEntropy::new();

        // A blank title falls back to 提醒, and a non-string is stringified.
        let mut arguments = Map::new();
        arguments.insert("title".to_string(), json!("   "));
        arguments.insert("dueAt".to_string(), json!("2026-09-15T10:30:00Z"));
        let created = create_reminder(&arguments, &root, &entropy).unwrap();
        assert_eq!(created["title"], "提醒");
        assert_eq!(created["content"], "");
        assert_eq!(created["notified"], false);

        let mut arguments = Map::new();
        arguments.insert("title".to_string(), json!(42));
        arguments.insert("content".to_string(), json!(null));
        arguments.insert("dueAt".to_string(), json!("2026-09-15T10:30:00Z"));
        let created = create_reminder(&arguments, &root, &entropy).unwrap();
        assert_eq!(created["title"], "42");
        assert_eq!(created["content"], "");

        // Truncation is by code point.
        let mut arguments = Map::new();
        arguments.insert("title".to_string(), json!("x".repeat(200)));
        arguments.insert("content".to_string(), json!("y".repeat(3000)));
        arguments.insert("dueAt".to_string(), json!("2026-09-15T10:30:00Z"));
        let created = create_reminder(&arguments, &root, &entropy).unwrap();
        assert_eq!(created["title"].as_str().unwrap().chars().count(), 120);
        assert_eq!(created["content"].as_str().unwrap().chars().count(), 2000);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn create_accepts_the_snake_case_due_key_and_requires_one() {
        let root = temp_root("create-due");
        let entropy = FixedEntropy::new();
        let mut arguments = Map::new();
        arguments.insert("due_at".to_string(), json!("2026-09-15T10:30:00Z"));
        let created = create_reminder(&arguments, &root, &entropy).unwrap();
        assert_eq!(created["dueAt"], "2026-09-15T10:30:00+00:00");

        let mut missing = Map::new();
        missing.insert("title".to_string(), json!("t"));
        let failure = create_reminder(&missing, &root, &entropy).unwrap_err();
        assert_eq!(failure.message, "Reminder dueAt is required");
        let _ = std::fs::remove_dir_all(&root);
    }

    /// A create drops already-notified entries from the store.
    #[test]
    fn create_prunes_notified_entries() {
        let root = temp_root("prune");
        let directory = reminders_dir(&root);
        std::fs::create_dir_all(&directory).unwrap();
        std::fs::write(
            reminders_file(&root),
            "[{\"id\": \"n1\", \"notified\": true}, {\"id\": \"a1\", \"notified\": false}]",
        )
        .unwrap();
        let entropy = FixedEntropy::new();
        let mut arguments = Map::new();
        arguments.insert("dueAt".to_string(), json!("2026-09-15T10:30:00Z"));
        create_reminder(&arguments, &root, &entropy).unwrap();

        let ids: Vec<String> = load_reminders(&root)
            .iter()
            .map(|item| item["id"].as_str().unwrap_or_default().to_string())
            .collect();
        assert!(!ids.contains(&"n1".to_string()));
        assert!(ids.contains(&"a1".to_string()));
        let _ = std::fs::remove_dir_all(&root);
    }

    // --- the branches ---------------------------------------------------------

    #[test]
    fn list_normalises_the_status_and_reports_an_untruncated_count() {
        let root = temp_root("list");
        let directory = reminders_dir(&root);
        std::fs::create_dir_all(&directory).unwrap();
        std::fs::write(
            reminders_file(&root),
            "[{\"id\": \"n1\", \"notified\": true}, {\"id\": \"a1\", \"notified\": false}]",
        )
        .unwrap();

        let mut active = Map::new();
        active.insert("status".to_string(), json!("active"));
        let result = list_reminders(&active, &root);
        assert_eq!(result["status"], "active");
        assert_eq!(result["count"], 1);
        assert_eq!(result["reminders"][0]["id"], "a1");

        let mut notified = Map::new();
        notified.insert("status".to_string(), json!("  NOTIFIED  "));
        let result = list_reminders(&notified, &root);
        assert_eq!(result["status"], "notified");
        assert_eq!(result["count"], 1);
        assert_eq!(result["reminders"][0]["id"], "n1");

        // An unknown status silently becomes "active".
        let mut bogus = Map::new();
        bogus.insert("status".to_string(), json!("bogus"));
        let result = list_reminders(&bogus, &root);
        assert_eq!(result["status"], "active");
        assert_eq!(result["count"], 1);

        // `all` keeps both, and a non-string status falls back too.
        let mut all = Map::new();
        all.insert("status".to_string(), json!("all"));
        assert_eq!(list_reminders(&all, &root)["count"], 2);
        let mut numeric = Map::new();
        numeric.insert("status".to_string(), json!(7));
        assert_eq!(list_reminders(&numeric, &root)["status"], "active");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn list_caps_the_returned_array_but_not_the_count() {
        let root = temp_root("list-cap");
        let directory = reminders_dir(&root);
        std::fs::create_dir_all(&directory).unwrap();
        let items: Vec<Value> = (0..60)
            .map(|index| json!({"id": format!("a{index}"), "notified": false}))
            .collect();
        std::fs::write(
            reminders_file(&root),
            serde_json::to_string(&Value::Array(items)).unwrap(),
        )
        .unwrap();

        let mut arguments = Map::new();
        arguments.insert("status".to_string(), json!("active"));
        let result = list_reminders(&arguments, &root);
        assert_eq!(result["reminders"].as_array().unwrap().len(), 50);
        assert_eq!(result["count"], 60);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn delete_reports_whether_anything_was_removed() {
        let root = temp_root("delete");
        let directory = reminders_dir(&root);
        std::fs::create_dir_all(&directory).unwrap();
        std::fs::write(
            reminders_file(&root),
            "[{\"id\": \"keep\"}, {\"id\": \"drop\"}]",
        )
        .unwrap();

        assert_eq!(delete_reminder("drop", &root).unwrap(), 1);
        assert_eq!(delete_reminder("nope", &root).unwrap(), 0);
        // A blank id is not an error, it is simply a no-op.
        assert_eq!(delete_reminder("   ", &root).unwrap(), 0);
        let remaining = load_reminders(&root);
        assert_eq!(remaining.len(), 1);
        assert_eq!(remaining[0]["id"], "keep");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn due_reminders_marks_and_returns_only_the_newly_due() {
        let root = temp_root("due");
        let directory = reminders_dir(&root);
        std::fs::create_dir_all(&directory).unwrap();
        std::fs::write(
            reminders_file(&root),
            "[{\"id\": \"past\", \"dueAt\": \"2026-01-01T00:00:00+00:00\", \"notified\": false}, \
              {\"id\": \"future\", \"dueAt\": \"2027-01-01T00:00:00+00:00\", \"notified\": false}, \
              {\"id\": \"bad\", \"dueAt\": \"nope\", \"notified\": false}]",
        )
        .unwrap();

        // 2026-06-01T00:00:00Z
        let now = 1_780_272_000_000;
        let due = due_reminders(&root, now).unwrap();
        assert_eq!(due.len(), 1);
        assert_eq!(due[0]["id"], "past");
        assert_eq!(due[0]["notified"], true);
        assert_eq!(due[0]["notifiedAt"], now);

        // The store is rewritten, so a second call finds nothing new.
        assert!(due_reminders(&root, now).unwrap().is_empty());
        let stored = load_reminders(&root);
        let past = stored.iter().find(|item| item["id"] == "past").unwrap();
        assert_eq!(past["notified"], true);
        // The unparseable entry is skipped, not fatal.
        assert!(stored.iter().any(|item| item["id"] == "bad"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn system_entropy_produces_sixteen_hex_characters_and_is_unique() {
        let entropy = SystemEntropy;
        let first = entropy.new_id().unwrap();
        let second = entropy.new_id().unwrap();
        assert_eq!(first.len(), 16);
        assert!(first.bytes().all(|byte| byte.is_ascii_hexdigit()));
        assert_ne!(first, second);
        assert!(entropy.now_millis() > 1_600_000_000_000);
    }
}
