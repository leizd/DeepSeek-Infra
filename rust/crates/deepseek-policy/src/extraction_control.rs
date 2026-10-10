//! Cancellation follows the blocking file parser and its platform RPCs.
use std::cell::RefCell;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use crate::app_error::{AppError, codes};

thread_local! {
    static CURRENT: RefCell<Option<FileProcessingControl>> = const { RefCell::new(None) };
}

#[derive(Clone, Default)]
pub struct FileProcessingControl(Arc<Control>);

#[derive(Default)]
struct Control {
    cancelled: AtomicBool,
    changed: tokio::sync::Notify,
}

impl FileProcessingControl {
    pub fn cancel(&self) {
        self.0.cancelled.store(true, Ordering::Release);
        self.0.changed.notify_waiters();
    }

    pub fn cancel_on_drop(&self) -> CancelFileProcessingOnDrop {
        CancelFileProcessingOnDrop(self.clone())
    }

    pub fn run<T>(&self, operation: impl FnOnce() -> T) -> T {
        struct Restore(Option<FileProcessingControl>);
        impl Drop for Restore {
            fn drop(&mut self) {
                CURRENT.with(|current| *current.borrow_mut() = self.0.take());
            }
        }
        let _restore = Restore(CURRENT.with(|current| current.replace(Some(self.clone()))));
        operation()
    }

    pub(crate) fn current() -> Option<Self> {
        CURRENT.with(|current| current.borrow().clone())
    }

    pub(crate) async fn cancelled(&self) {
        let changed = self.0.changed.notified();
        tokio::pin!(changed);
        changed.as_mut().enable();
        if !self.0.cancelled.load(Ordering::Acquire) {
            changed.await;
        }
    }
}

pub struct CancelFileProcessingOnDrop(FileProcessingControl);
impl Drop for CancelFileProcessingOnDrop {
    fn drop(&mut self) {
        self.0.cancel();
    }
}

pub(crate) fn cancelled_error() -> AppError {
    AppError {
        message: "File processing cancelled".into(),
        code: codes::INVALID_REQUEST,
        status: 499,
    }
}

pub(crate) fn check() -> Result<(), AppError> {
    if FileProcessingControl::current()
        .is_some_and(|control| control.0.cancelled.load(Ordering::Acquire))
    {
        Err(cancelled_error())
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cancellation_before_extraction_does_not_create_a_cache() {
        let root = tempfile::tempdir().unwrap();
        let control = FileProcessingControl::default();
        control.cancel();
        let error = control
            .run(|| {
                crate::file_upload::extract_uploaded_file(
                    root.path(),
                    "sample.txt",
                    "text/plain",
                    b"readable retained text",
                    false,
                    None,
                )
            })
            .unwrap_err();
        assert_eq!(error.status, 499);
        assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 0);
        assert!(check().is_ok());
    }

    #[tokio::test]
    async fn dropped_handler_signals_both_pending_and_future_waiters() {
        let control = FileProcessingControl::default();
        let guard = control.cancel_on_drop();
        let waiter = control.cancelled();
        tokio::pin!(waiter);
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(1), &mut waiter)
                .await
                .is_err()
        );
        drop(guard);
        tokio::time::timeout(std::time::Duration::from_millis(100), waiter)
            .await
            .unwrap();
        tokio::time::timeout(std::time::Duration::from_millis(100), control.cancelled())
            .await
            .unwrap();
    }
}
