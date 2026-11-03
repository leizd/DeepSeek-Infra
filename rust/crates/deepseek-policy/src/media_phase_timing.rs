//! Opt-in timings only. No record, claim, path, key or authority data is logged.
use std::{
    io::Write as _,
    sync::{
        OnceLock,
        atomic::{AtomicU64, Ordering},
    },
    time::Instant,
};

pub struct Phase {
    label: &'static str,
    id: u64,
    started: Option<Instant>,
}
impl Phase {
    pub fn start(label: &'static str) -> Self {
        static ENABLED: OnceLock<bool> = OnceLock::new();
        static NEXT: AtomicU64 = AtomicU64::new(1);
        let enabled = *ENABLED.get_or_init(|| {
            std::env::var("DEEPSEEK_MEDIA_PHASE_TIMING").is_ok_and(|value| value == "1")
        });
        if !enabled {
            return Self {
                label,
                id: 0,
                started: None,
            };
        }
        let id = NEXT.fetch_add(1, Ordering::Relaxed);
        let started = Instant::now();
        let _ = writeln!(
            std::io::stderr().lock(),
            "MEDIA_PHASE event=enter pid={} id={id} label={label}",
            std::process::id()
        );
        Self {
            label,
            id,
            started: Some(started),
        }
    }
}
impl Drop for Phase {
    fn drop(&mut self) {
        if let Some(started) = self.started {
            // Scope exit is a timing observation, never evidence of success or
            // an applied effect. Errors and unwinding also leave the scope.
            let _ = writeln!(
                std::io::stderr().lock(),
                "MEDIA_PHASE event=exit pid={} id={} label={} elapsed_us={}",
                std::process::id(),
                self.id,
                self.label,
                started.elapsed().as_micros()
            );
        }
    }
}
