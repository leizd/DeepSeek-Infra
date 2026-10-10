//! The machine's clock and zone, resolved into [`LocalNow`].
//!
//! `dynamic_context` needs three facts the oracle reads off the host — the instant, the local
//! UTC offset and the zone's own name — and its module doc says as much: `LocalNow` carries
//! them and "resolving the OS zone is a wiring concern that is **not** implemented here".
//! This module is that wiring. It is non-pure, so it lives in the gateway beside the other
//! injected dependencies (the search transport, the upstream client) rather than in the policy
//! crate next to the formatter that consumes it.
//!
//! Nothing in this workspace calls [`system_local_now`] yet. The assembly's production caller
//! is the next slice, so this lands additive and inert on purpose: the one thing it changes is
//! that the clock stops being the reason that caller cannot be written.
//!
//! # Which OS read, and why not a time crate
//!
//! The parity target is `datetime.now().astimezone()`, and CPython does not consult a tz
//! database there — `datetime._local_timezone()` asks the C library the program already links:
//!
//! - **Windows**: `tm_gmtoff`/`tm_zone` are POSIX-only fields and are absent, so CPython falls
//!   back to `time.timezone` / `time.altzone` and `time.tzname[tm_isdst]`, which the UCRT fills
//!   from `GetTimeZoneInformation`.
//! - **POSIX**: it takes `localtime_r`'s `tm_gmtoff` and `tm_zone` directly.
//!
//! So agreeing with the oracle means asking the same C library. A time crate would not close the
//! gap either: the offset is easy to reproduce, but the **name** is printed into the prompt
//! (`format_current_time_context` renders `Local time: … (<name>)`), and neither `chrono` nor
//! `time` exposes the C library's zone name.
//!
//! Measured on the development host (a zh-CN Windows 11, 2026-09-18): `GetTimeZoneInformation`
//! reports `Bias = -480`, `StandardBias = 0`, `StandardName = "中国标准时间"`, and CPython's
//! `tzname()` returns exactly that string — **not** the English `China Standard Time`, which is
//! the example `dynamic_context` used to lead a reader to expect, and which would have made a
//! hand-written table look correct while the bytes diverged on every request.
//!
//! # The FFI is declared, not dependenc-ised
//!
//! `deepseek-policy::file_lock` set this pattern: `#[link(name = "kernel32")]` on Windows, a bare
//! `extern "C"` on Unix, and the C types written out as `#[repr(C)]` structs. It keeps the
//! dependency graph exactly as it is, which is what this workspace pins for.
//!
//! # What every platform can and cannot see
//!
//! `GetTimeZoneInformation` classifies **the current time** — it has no "at this instant" form —
//! while `localtime_r` resolves the zone *for the instant you hand it*. [`SystemZone::local_now`]
//! therefore carries a caller-supplied instant on both platforms, and [`system_local_now`] passes
//! the current one; a caller pinning a different instant (the parity probe) is pinning the
//! timestamp, not the offset, and on Windows the offset it gets is the one for *now*.
//!
//! # Verified
//!
//! `tasks/native-runtime/local_clock_parity_probe.py` pairs with
//! `examples/local_clock_parity_probe.rs`: the Python side resolves the zone with CPython's own
//! path and the Rust side with this module, both rendering the same pinned instant through
//! `format_current_time_context`, and the outputs are compared byte for byte. The unit tests
//! below pin the Windows bias arithmetic — including the daylight and non-zero-`StandardBias`
//! branches, which no host in reach exercises — so the mapping is tested on every platform and
//! not only on the one CI happens to run on.

use std::time::{SystemTime, UNIX_EPOCH};

use deepseek_policy::dynamic_context::LocalNow;

/// Why the OS could not be asked for the machine's zone.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LocalClockError {
    /// This target has no implementation. The two that do are Windows and Unix; anything else
    /// fails closed rather than quietly reporting UTC.
    UnsupportedPlatform,
    /// The OS was asked and refused: `GetTimeZoneInformation` returned `TIME_ZONE_ID_INVALID`,
    /// or `localtime_r` returned null (an instant the C library cannot represent).
    ZoneInformationUnavailable,
}

impl std::fmt::Display for LocalClockError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnsupportedPlatform => write!(f, "the OS-local zone is not implemented here"),
            Self::ZoneInformationUnavailable => {
                write!(f, "the OS reported no time-zone information")
            }
        }
    }
}

impl std::error::Error for LocalClockError {}

/// The machine's zone: the offset, the zone's own name, and whether the OS is classifying the
/// current time as daylight-saving.
///
/// `is_daylight` is carried rather than folded away because it is what makes a name mismatch
/// diagnosable — a wrong `daylight_bias` picks a *different* name, and without this field the
/// only symptom would be a diff.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SystemZone {
    pub offset_seconds: i32,
    pub timezone_name: String,
    pub is_daylight: bool,
}

impl SystemZone {
    /// The zone applied to an instant, in the shape `dynamic_context` consumes.
    pub fn local_now(&self, epoch_seconds: i64) -> LocalNow {
        LocalNow {
            epoch_seconds,
            offset_seconds: self.offset_seconds,
            timezone_name: self.timezone_name.clone(),
        }
    }
}

/// The five numbers `GetTimeZoneInformation` returns, as data.
///
/// Deliberately **not** `cfg`-gated: the OS read is the only part that has to be
/// platform-specific, so keeping the record and its mapping platform-neutral means the
/// arithmetic below is unit-tested on Linux CI as well as on Windows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WindowsZoneFacts {
    /// `Bias`: minutes **west** of UTC, the opposite sign to a UTC offset.
    pub bias_minutes: i32,
    /// `StandardBias`, normally 0.
    pub standard_bias_minutes: i32,
    /// `DaylightBias`, normally -60.
    pub daylight_bias_minutes: i32,
    /// Whether the API classified the current time as daylight-saving.
    pub is_daylight: bool,
    pub standard_name: String,
    pub daylight_name: String,
}

impl WindowsZoneFacts {
    /// The effective offset is `-(Bias + BiasForThePeriod)` minutes, and the name is the one
    /// belonging to the same period — the two must be chosen together, which is why this is one
    /// function rather than two accessors.
    pub fn zone(&self) -> SystemZone {
        let (bias_minutes, name) = if self.is_daylight {
            (self.daylight_bias_minutes, &self.daylight_name)
        } else {
            (self.standard_bias_minutes, &self.standard_name)
        };
        SystemZone {
            offset_seconds: -(self.bias_minutes + bias_minutes) * 60,
            timezone_name: name.clone(),
            is_daylight: self.is_daylight,
        }
    }
}

/// Ask the OS for the machine's zone, the way the oracle does.
pub fn system_zone() -> Result<SystemZone, LocalClockError> {
    imp::system_zone()
}

/// The machine's zone applied to the current instant — what a request assembler needs.
pub fn system_local_now() -> Result<LocalNow, LocalClockError> {
    system_zone().map(|zone| zone.local_now(now_epoch_seconds()))
}

/// Whole seconds since the Unix epoch, in UTC. `SystemTime` cannot go negative in practice, but
/// a clock set before 1970 can, and the oracle's `epoch_seconds` is a signed count.
fn now_epoch_seconds() -> i64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(elapsed) => elapsed.as_secs() as i64,
        Err(before) => -(before.duration().as_secs() as i64),
    }
}

#[cfg(windows)]
mod imp {
    use super::{LocalClockError, SystemZone, WindowsZoneFacts};

    /// `SYSTEMTIME`.
    #[repr(C)]
    #[derive(Default, Clone, Copy)]
    struct SystemTime {
        year: u16,
        month: u16,
        day_of_week: u16,
        day: u16,
        hour: u16,
        minute: u16,
        second: u16,
        milliseconds: u16,
    }

    /// `TIME_ZONE_INFORMATION`: 172 bytes, the same on 32- and 64-bit Windows because every
    /// member is naturally aligned.
    #[repr(C)]
    struct TimeZoneInformation {
        bias: i32,
        standard_name: [u16; 32],
        standard_date: SystemTime,
        standard_bias: i32,
        daylight_name: [u16; 32],
        daylight_date: SystemTime,
        daylight_bias: i32,
    }

    impl TimeZoneInformation {
        fn empty() -> Self {
            Self {
                bias: 0,
                standard_name: [0; 32],
                standard_date: SystemTime::default(),
                standard_bias: 0,
                daylight_name: [0; 32],
                daylight_date: SystemTime::default(),
                daylight_bias: 0,
            }
        }
    }

    // WinAPI is stdcall on 32-bit x86 and the C ABI on x64, which is what `"system"` means;
    // declaring these as `"C"` would only be equivalent on the 64-bit targets.
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GetTimeZoneInformation(info: *mut TimeZoneInformation) -> u32;
    }

    /// `TIME_ZONE_ID_INVALID`.
    const TIME_ZONE_ID_INVALID: u32 = 0xFFFF_FFFF;
    /// `TIME_ZONE_ID_DAYLIGHT`. `TIME_ZONE_ID_UNKNOWN` (0) and `TIME_ZONE_ID_STANDARD` (1) both
    /// mean "not daylight" — `UNKNOWN` is what a zone with no DST rules returns.
    const TIME_ZONE_ID_DAYLIGHT: u32 = 2;

    pub fn system_zone() -> Result<SystemZone, LocalClockError> {
        let mut info = TimeZoneInformation::empty();
        let state = unsafe { GetTimeZoneInformation(&mut info) };
        if state == TIME_ZONE_ID_INVALID {
            return Err(LocalClockError::ZoneInformationUnavailable);
        }
        Ok(WindowsZoneFacts {
            bias_minutes: info.bias,
            standard_bias_minutes: info.standard_bias,
            daylight_bias_minutes: info.daylight_bias,
            is_daylight: state == TIME_ZONE_ID_DAYLIGHT,
            standard_name: wide_string(&info.standard_name),
            daylight_name: wide_string(&info.daylight_name),
        }
        .zone())
    }

    /// The names are NUL-terminated `WCHAR[32]` buffers, and the whole buffer can be full.
    fn wide_string(buffer: &[u16; 32]) -> String {
        let end = buffer
            .iter()
            .position(|&unit| unit == 0)
            .unwrap_or(buffer.len());
        String::from_utf16_lossy(&buffer[..end])
    }
}

#[cfg(unix)]
mod imp {
    use std::ffi::{CStr, c_char, c_int, c_long};

    use super::{LocalClockError, SystemZone};

    /// `struct tm`. The first nine fields are the C standard's; `tm_gmtoff` and `tm_zone` are
    /// the BSD/glibc extensions that POSIX later standardised, and they are what CPython reads.
    /// glibc and musl lay these out identically on the 64-bit targets this workspace builds for.
    #[repr(C)]
    struct Tm {
        sec: c_int,
        min: c_int,
        hour: c_int,
        mday: c_int,
        mon: c_int,
        year: c_int,
        wday: c_int,
        yday: c_int,
        isdst: c_int,
        gmtoff: c_long,
        zone: *const c_char,
    }

    unsafe extern "C" {
        fn localtime_r(time: *const c_long, result: *mut Tm) -> *mut Tm;
    }

    pub fn system_zone() -> Result<SystemZone, LocalClockError> {
        let now: c_long = super::now_epoch_seconds() as c_long;
        let mut broken_down = Tm {
            sec: 0,
            min: 0,
            hour: 0,
            mday: 0,
            mon: 0,
            year: 0,
            wday: 0,
            yday: 0,
            isdst: 0,
            gmtoff: 0,
            zone: std::ptr::null(),
        };
        // `localtime_r` is the reentrant form; plain `localtime` shares a static buffer.
        let filled = unsafe { localtime_r(&now, &mut broken_down) };
        if filled.is_null() {
            return Err(LocalClockError::ZoneInformationUnavailable);
        }
        let timezone_name = if broken_down.zone.is_null() {
            String::new()
        } else {
            unsafe { CStr::from_ptr(broken_down.zone) }
                .to_string_lossy()
                .into_owned()
        };
        Ok(SystemZone {
            offset_seconds: broken_down.gmtoff as i32,
            timezone_name,
            is_daylight: broken_down.isdst > 0,
        })
    }
}

#[cfg(not(any(windows, unix)))]
mod imp {
    use super::{LocalClockError, SystemZone};

    pub fn system_zone() -> Result<SystemZone, LocalClockError> {
        Err(LocalClockError::UnsupportedPlatform)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn facts(is_daylight: bool) -> WindowsZoneFacts {
        WindowsZoneFacts {
            bias_minutes: -480,
            standard_bias_minutes: 0,
            daylight_bias_minutes: -60,
            is_daylight,
            standard_name: "中国标准时间".to_string(),
            daylight_name: "中国夏令时".to_string(),
        }
    }

    /// The values measured on the development host: `Bias = -480` minutes is UTC+8, and the
    /// sign has to be flipped twice on the way out (minutes-west-of-UTC, then per-minute).
    #[test]
    fn the_measured_host_zone_maps_to_its_offset_and_name() {
        let zone = facts(false).zone();
        assert_eq!(zone.offset_seconds, 28800);
        assert_eq!(zone.timezone_name, "中国标准时间");
        assert!(!zone.is_daylight);
    }

    /// No host in reach has DST, so this branch is pinned here instead: `DaylightBias = -60`
    /// adds an hour *to* the offset, and the name has to move with it.
    #[test]
    fn daylight_picks_the_daylight_bias_and_the_daylight_name_together() {
        let zone = facts(true).zone();
        assert_eq!(zone.offset_seconds, 32400);
        assert_eq!(zone.timezone_name, "中国夏令时");
        assert!(zone.is_daylight);
    }

    /// `StandardBias` is documented as "usually 0" and is 0 on this host, so a wrong formula
    /// would still pass everywhere in reach. The non-zero case is pinned here instead:
    /// `-(-480 + -30) * 60 = 30600`.
    ///
    /// Windows ignores `StandardBias` when no `StandardDate` is supplied, which is not modelled:
    /// that combination does not arise on a real zone (an unsupplied transition date is what
    /// makes the API answer `TIME_ZONE_ID_UNKNOWN`, and it reports a zero `StandardBias` with it),
    /// and the paired probe reads whatever the host actually reports, so a host where it did
    /// matter would show up as an offset mismatch rather than as a silent one.
    #[test]
    fn a_non_zero_standard_bias_is_part_of_the_offset() {
        let mut record = facts(false);
        record.standard_bias_minutes = -30;
        assert_eq!(record.zone().offset_seconds, 30600);
    }

    #[test]
    fn utc_maps_to_zero_with_its_own_name() {
        let record = WindowsZoneFacts {
            bias_minutes: 0,
            standard_bias_minutes: 0,
            daylight_bias_minutes: 0,
            is_daylight: false,
            standard_name: "UTC".to_string(),
            daylight_name: String::new(),
        };
        let zone = record.zone();
        assert_eq!(zone.offset_seconds, 0);
        assert_eq!(zone.timezone_name, "UTC");
    }

    /// An empty name is legal (the formatter falls back to `local`), and `local_now` must carry
    /// the caller's instant through untouched rather than reading a clock of its own.
    #[test]
    fn local_now_carries_the_instant_it_is_given() {
        let zone = SystemZone {
            offset_seconds: 28800,
            timezone_name: String::new(),
            is_daylight: false,
        };
        let now = zone.local_now(1_758_096_268);
        assert_eq!(now.epoch_seconds, 1_758_096_268);
        assert_eq!(now.offset_seconds, 28800);
        assert!(now.timezone_name.is_empty());
    }

    /// The host read itself: this is not a comparison against the oracle (the parity probe is),
    /// only that the OS answers at all on every platform this builds for.
    #[test]
    fn the_host_answers_with_a_representable_offset() {
        let zone = system_zone().expect("the host reports a zone");
        assert!(
            zone.offset_seconds.abs() <= 18 * 3600,
            "offset {}",
            zone.offset_seconds
        );
        let now = system_local_now().expect("the host reports a zone");
        assert!(now.offset_seconds.abs() <= 18 * 3600);
    }
}
