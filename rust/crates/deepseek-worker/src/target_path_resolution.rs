//! Retained pathlib.resolve(strict=False) semantics for registry overlap reads.
//! This does not assert existence, initialize a marker, or reconcile an effect.
use std::{
    fs,
    path::{Component, Path, PathBuf},
};
use tonic::Status;

const MAX_PATH: usize = 32768;
const MAX_LINKS: usize = 1024;
const MAX_RETAINED_PATH_BYTES: usize = 8 * 1024 * 1024;

fn unresolved() -> Status {
    Status::unavailable("BACKUP_TARGET_PATH_UNRESOLVED")
}

pub(super) fn resolve(raw: &str) -> Result<String, Status> {
    if raw.len() > MAX_PATH || raw.contains('\0') || !Path::new(raw).is_absolute() {
        return Err(Status::invalid_argument("BACKUP_TARGET_LOCATION_INVALID"));
    }
    let resolved = resolve_platform(Path::new(raw))?;
    #[cfg(windows)]
    if let Err(error) = fs::metadata(&resolved) {
        if error.raw_os_error() == Some(1921) {
            return Err(unresolved());
        }
    }
    let value = resolved.to_str().ok_or_else(unresolved)?;
    if value.len() > MAX_PATH || !resolved.is_absolute() {
        return Err(unresolved());
    }
    Ok(value.into())
}

fn lexical(path: &Path) -> PathBuf {
    let mut result = PathBuf::new();
    for part in path.components() {
        match part {
            Component::CurDir => (),
            Component::ParentDir => {
                result.pop();
            }
            _ => result.push(part.as_os_str()),
        }
    }
    result
}

#[cfg(not(windows))]
fn resolve_platform(path: &Path) -> Result<PathBuf, Status> {
    use std::{
        collections::{HashMap, VecDeque},
        ffi::OsString,
    };
    enum Part {
        Name(OsString),
        Completed(PathBuf),
    }
    fn parts(path: &Path) -> Vec<Part> {
        path.components()
            .map(|c| Part::Name(c.as_os_str().into()))
            .collect()
    }
    let mut remaining = VecDeque::from(parts(path));
    let mut resolved = PathBuf::new();
    let mut seen: HashMap<PathBuf, Option<PathBuf>> = HashMap::new();
    let mut retained_bytes = path.as_os_str().len();
    // Work remains on the heap, including long acyclic chains. The bound also
    // prevents expanding adversarial filesystem links without limit.
    let mut work = 0usize;
    while let Some(part) = remaining.pop_front() {
        work += 1;
        if work > MAX_PATH * 4 {
            return Err(unresolved());
        }
        let name = match part {
            Part::Completed(link) => {
                retained_bytes += resolved.as_os_str().len();
                if retained_bytes > MAX_RETAINED_PATH_BYTES {
                    return Err(unresolved());
                }
                seen.insert(link, Some(resolved.clone()));
                continue;
            }
            Part::Name(name) => name,
        };
        if name == "." {
            continue;
        }
        if name == ".." {
            resolved.pop();
            continue;
        }
        let candidate = resolved.join(&name);
        if candidate.as_os_str().len() > MAX_PATH {
            return Err(unresolved());
        }
        // CPython's non-strict realpath keeps the unresolved component on
        // lstat errors, including missing suffixes and non-directory parents.
        let linked = fs::symlink_metadata(&candidate)
            .map(|m| m.file_type().is_symlink())
            .unwrap_or(false);
        if !linked {
            resolved = candidate;
            continue;
        }
        if let Some(cached) = seen.get(&candidate) {
            if let Some(cached) = cached {
                resolved = cached.clone();
                continue;
            }
            // realpath retains the unresolved loop plus suffix, then normalizes
            // it. pathlib raises only if the final stat still encounters ELOOP.
            resolved = candidate;
            for part in remaining {
                if let Part::Name(name) = part {
                    resolved.push(name);
                }
            }
            resolved = lexical(&resolved);
            break;
        }
        let target = fs::read_link(&candidate).map_err(|_| unresolved())?;
        retained_bytes += 2 * candidate.as_os_str().len() + target.as_os_str().len();
        if target.as_os_str().len() > MAX_PATH
            || seen.len() >= MAX_LINKS
            || retained_bytes > MAX_RETAINED_PATH_BYTES
        {
            return Err(unresolved());
        }
        seen.insert(candidate.clone(), None);
        remaining.push_front(Part::Completed(candidate));
        for part in parts(&target).into_iter().rev() {
            remaining.push_front(part);
        }
    }
    if let Err(error) = fs::metadata(&resolved) {
        let eloop = if cfg!(target_os = "macos") { 62 } else { 40 };
        if error.raw_os_error() == Some(eloop) {
            return Err(unresolved());
        }
    }
    Ok(resolved)
}

#[cfg(windows)]
fn resolve_platform(path: &Path) -> Result<PathBuf, Status> {
    use std::collections::HashSet;
    fn allowed(error: &std::io::Error) -> bool {
        matches!(
            error.raw_os_error(),
            Some(1 | 2 | 3 | 5 | 21 | 32 | 50 | 53 | 65 | 67 | 87 | 123 | 161 | 1920 | 1921)
        )
    }
    fn deep_link(path: &Path) -> Result<PathBuf, Status> {
        let mut current = path.to_path_buf();
        let mut seen = HashSet::new();
        let mut retained_bytes = 0usize;
        while seen.insert(current.to_string_lossy().to_lowercase()) {
            retained_bytes += current.as_os_str().len();
            if seen.len() > MAX_LINKS || retained_bytes > MAX_RETAINED_PATH_BYTES {
                return Err(unresolved());
            }
            match fs::read_link(&current) {
                Ok(target) => {
                    current = if target.is_absolute() {
                        target
                    } else {
                        lexical(&current.parent().ok_or_else(unresolved)?.join(target))
                    };
                }
                Err(error)
                    if allowed(&error) || error.kind() == std::io::ErrorKind::InvalidInput =>
                {
                    break;
                }
                Err(_) => return Err(unresolved()),
            }
        }
        Ok(current)
    }
    // ntpath realpath normalizes dot/parent components before following links.
    let normalized = lexical(path);
    let initial = fs::canonicalize(&normalized);
    let initial_error = initial.as_ref().err().and_then(|e| e.raw_os_error());
    let resolved = match initial {
        Ok(value) => value,
        Err(_) => {
            let mut current = normalized.clone();
            let mut tail = Vec::new();
            loop {
                match fs::canonicalize(&current) {
                    Ok(mut base) => {
                        for name in tail.into_iter().rev() {
                            base.push(name);
                        }
                        break base;
                    }
                    Err(error) if allowed(&error) => (),
                    Err(_) => return Err(unresolved()),
                }
                if let Ok(mut linked) = deep_link(&current) {
                    if linked != current {
                        for name in tail.into_iter().rev() {
                            linked.push(name);
                        }
                        break linked;
                    }
                }
                let Some(name) = current.file_name().map(|n| n.to_os_string()) else {
                    for name in tail.into_iter().rev() {
                        current.push(name);
                    }
                    break current;
                };
                tail.push(name);
                if !current.pop() {
                    return Err(unresolved());
                }
            }
        }
    };
    let text = resolved.to_str().ok_or_else(unresolved)?;
    if path.to_string_lossy().starts_with(r"\\?\") {
        return Ok(resolved);
    }
    let unprefixed = text
        .strip_prefix(r"\\?\UNC\")
        .map(|s| format!(r"\\{s}"))
        .or_else(|| text.strip_prefix(r"\\?\").map(str::to_owned));
    if let Some(unprefixed) = unprefixed {
        match fs::canonicalize(&unprefixed) {
            Ok(value) if value == resolved => return Ok(unprefixed.into()),
            Err(error) if initial_error.is_some() && error.raw_os_error() == initial_error => {
                return Ok(unprefixed.into());
            }
            _ => (),
        }
    }
    Ok(resolved)
}

#[cfg(test)]
#[path = "target_path_resolution_tests.rs"]
mod tests;
