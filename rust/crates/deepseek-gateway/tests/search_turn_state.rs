//! A request's search budget and citation numbers must survive tool rounds.
use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::thread;
use std::time::Duration;

use deepseek_gateway::chat_tool_loop::{ToolRoundExecutor, WorkspaceBundle};
use serde_json::{Value, json};

struct OwnedSearch {
    url: String,
    requests: Arc<AtomicUsize>,
    stop: Arc<AtomicBool>,
    task: Option<thread::JoinHandle<()>>,
}

impl OwnedSearch {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let url = format!("http://{}/owned-search", listener.local_addr().unwrap());
        let stop = Arc::new(AtomicBool::new(false));
        let requests = Arc::new(AtomicUsize::new(0));
        let stopping = stop.clone();
        let count = requests.clone();
        let task = thread::spawn(move || {
            while !stopping.load(Ordering::Acquire) {
                let (mut socket, _) = match listener.accept() {
                    Ok(value) => value,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                        continue;
                    }
                    Err(error) => panic!("owned search accept failed: {error}"),
                };
                // Accepted sockets inherit nonblocking mode on Windows.
                socket.set_nonblocking(false).unwrap();
                socket
                    .set_read_timeout(Some(Duration::from_secs(3)))
                    .unwrap();
                let mut raw = Vec::new();
                let mut chunk = [0; 4096];
                let head_end = loop {
                    let length = socket.read(&mut chunk).unwrap();
                    assert!(length > 0);
                    raw.extend_from_slice(&chunk[..length]);
                    if let Some(index) = raw.windows(4).position(|value| value == b"\r\n\r\n") {
                        break index + 4;
                    }
                    assert!(raw.len() < 65536);
                };
                let head = String::from_utf8_lossy(&raw[..head_end]);
                assert!(head.starts_with("POST /owned-search "));
                assert!(
                    head.to_lowercase()
                        .contains("authorization: bearer owned-search-key")
                );
                let length: usize = head
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse().unwrap())
                    })
                    .unwrap();
                while raw.len() < head_end + length {
                    let length = socket.read(&mut chunk).unwrap();
                    assert!(length > 0);
                    raw.extend_from_slice(&chunk[..length]);
                }
                let payload: Value =
                    serde_json::from_slice(&raw[head_end..head_end + length]).unwrap();
                assert!(
                    payload["query"]
                        .as_str()
                        .is_some_and(|query| !query.is_empty())
                );
                let index = count.fetch_add(1, Ordering::SeqCst) + 1;
                let body = json!({"results": [{"title": format!("Owned source {index}"),
                    "url": format!("https://example.org/owned-{index}"), "content": "owned retained source", "score": 1.0}]}).to_string();
                write!(socket, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
            }
        });
        Self {
            url,
            requests,
            stop,
            task: Some(task),
        }
    }
}

impl Drop for OwnedSearch {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Err(error) = self.task.take().unwrap().join() {
            if !thread::panicking() {
                std::panic::resume_unwind(error);
            }
        }
    }
}

struct Environment(Vec<(&'static str, Option<String>)>);
impl Environment {
    fn set(url: &str) -> Self {
        let values = [
            ("TAVILY_API_URL", url),
            ("TAVILY_API_KEY", "owned-search-key"),
            ("WEB_SEARCH_TURN_LIMIT", "2"),
        ];
        let previous = values
            .iter()
            .map(|(name, _)| (*name, std::env::var(name).ok()))
            .collect();
        for (name, value) in values {
            unsafe {
                std::env::set_var(name, value);
            }
        }
        Self(previous)
    }
}
impl Drop for Environment {
    fn drop(&mut self) {
        for (name, value) in &self.0 {
            unsafe {
                match value {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }
    }
}

fn call(query: &str) -> Value {
    json!({"id": format!("owned-{query}"), "type": "function",
        "function": {"name": "web_search", "arguments": json!({"query": query}).to_string()}})
}

fn result(messages: &[Value]) -> Value {
    assert_eq!(messages.len(), 1);
    let body: Value = serde_json::from_str(messages[0]["content"].as_str().unwrap()).unwrap();
    assert_eq!(body["ok"], true, "{body}");
    body["result"].clone()
}

#[tokio::test]
async fn search_turn_budget_citations_and_memo_survive_separate_tool_rounds() {
    let upstream = OwnedSearch::start();
    let _environment = Environment::set(&upstream.url);
    let root = tempfile::tempdir().unwrap();
    let executor =
        ToolRoundExecutor::new(Some(WorkspaceBundle::new(root.path().to_path_buf())), None);
    let first = result(&executor.run_round(vec![call("first owned query")]).await);
    let second = result(&executor.run_round(vec![call("second owned query")]).await);
    assert_eq!(upstream.requests.load(Ordering::SeqCst), 2);
    assert_eq!(first["round"], 1);
    assert_eq!(
        second["round"], 2,
        "search counter reset between model rounds: {second}"
    );
    assert_eq!(first["results"][0]["cite"], "[^W1]");
    assert_eq!(second["results"][0]["cite"], "[^W2]");
    let mut repeat_call = call("  FIRST owned query  ");
    repeat_call["id"] = json!("different-owned-repeat-call-id");
    let repeated = result(&executor.run_round(vec![repeat_call]).await);
    // A memo hit retains its original round/citations without consuming a turn.
    assert_eq!(repeated["status"], "done");
    assert_eq!(repeated["round"], 1);
    assert_eq!(repeated["results"][0]["cite"], "[^W1]");
    let limited = result(&executor.run_round(vec![call("third owned query")]).await);
    assert_eq!(limited["status"], "error");
    assert_eq!(
        limited["error"],
        deepseek_gateway::search_provider::WEB_SEARCH_LIMIT_ERROR
    );
    assert_eq!(
        upstream.requests.load(Ordering::SeqCst),
        2,
        "limit rejection reached the provider"
    );
    let sync = tokio::task::spawn_blocking(move || {
        executor.execute_call_sync("web_search", &json!({"query": "sync fourth owned query"}))
    })
    .await
    .unwrap();
    assert_eq!(sync["ok"], true);
    assert_eq!(
        sync["result"]["error"],
        deepseek_gateway::search_provider::WEB_SEARCH_LIMIT_ERROR
    );
    assert_eq!(
        upstream.requests.load(Ordering::SeqCst),
        2,
        "synchronous calls reset the same request's budget"
    );
    // A new request gets its own budget; request state must not become global.
    let next = ToolRoundExecutor::new(Some(WorkspaceBundle::new(root.path().to_path_buf())), None);
    let next_result = result(&next.run_round(vec![call("new request owned query")]).await);
    assert_eq!(next_result["round"], 1);
    assert_eq!(next_result["results"][0]["cite"], "[^W1]");
    assert_eq!(upstream.requests.load(Ordering::SeqCst), 3);
}
