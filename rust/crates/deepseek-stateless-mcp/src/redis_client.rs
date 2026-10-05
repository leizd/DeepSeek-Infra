use std::io::{Read, Write};
use std::net::TcpStream as StdTcpStream;
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

#[derive(Debug, Clone, PartialEq)]
pub enum RedisValue {
    Nil,
    Status(String),
    Int(i64),
    Bulk(String),
    Array(Vec<RedisValue>),
}

#[derive(Debug)]
pub struct RedisClient {
    stream: TcpStream,
    buffer: Vec<u8>,
}

impl RedisClient {
    pub async fn connect(url: &str) -> Result<Self, String> {
        let parsed = parse_redis_url(url)?;
        let stream = TcpStream::connect((parsed.host.as_str(), parsed.port))
            .await
            .map_err(|error| error.to_string())?;
        let mut client = Self {
            stream,
            buffer: Vec::new(),
        };
        if let Some(password) = parsed.password {
            client.call(&[b"AUTH", password.as_bytes()]).await?;
        }
        if parsed.database != 0 {
            client
                .call(&[b"SELECT", parsed.database.to_string().as_bytes()])
                .await?;
        }
        Ok(client)
    }

    pub async fn call(&mut self, args: &[&[u8]]) -> Result<RedisValue, String> {
        self.stream
            .write_all(&encode_command(args))
            .await
            .map_err(|error| error.to_string())?;
        self.stream
            .flush()
            .await
            .map_err(|error| error.to_string())?;
        loop {
            match decode_frame(&self.buffer)? {
                Some(frame) => {
                    self.buffer.drain(..frame.used);
                    return frame.value;
                }
                None => {
                    let mut chunk = [0u8; 4096];
                    let read = self
                        .stream
                        .read(&mut chunk)
                        .await
                        .map_err(|error| error.to_string())?;
                    if read == 0 {
                        return Err("redis connection closed".to_string());
                    }
                    self.buffer.extend_from_slice(&chunk[..read]);
                }
            }
        }
    }

    pub async fn ping(&mut self) -> Result<(), String> {
        match self.call(&[b"PING"]).await? {
            RedisValue::Status(text) if text == "PONG" => Ok(()),
            RedisValue::Bulk(text) if text == "PONG" => Ok(()),
            other => Err(format!("unexpected ping reply {other:?}")),
        }
    }
}

pub struct RedisUrl {
    pub host: String,
    pub port: u16,
    pub password: Option<String>,
    pub database: i64,
}

pub fn parse_redis_url(url: &str) -> Result<RedisUrl, String> {
    let rest = url
        .strip_prefix("redis://")
        .ok_or_else(|| "redis url must start with redis://".to_string())?;
    let (auth, hostport) = match rest.rsplit_once('@') {
        Some((auth, hostport)) => (Some(auth), hostport),
        None => (None, rest),
    };
    let (hostport, database) = match hostport.split_once('/') {
        Some((hostport, database)) => {
            let database = database
                .parse::<i64>()
                .map_err(|_| "redis database is not an integer".to_string())?;
            (hostport, database)
        }
        None => (hostport, 0),
    };
    let (host, port) = match hostport.rsplit_once(':') {
        Some((host, port)) => {
            let port = port
                .parse::<u16>()
                .map_err(|_| "redis port is not an integer".to_string())?;
            (host.to_string(), port)
        }
        None => (hostport.to_string(), 6379),
    };
    if host.is_empty() {
        return Err("redis host is empty".to_string());
    }
    let password = auth.and_then(|value| {
        let secret = value
            .split_once(':')
            .map(|(_, secret)| secret)
            .unwrap_or(value);
        let secret = secret.trim_start_matches(':');
        if secret.is_empty() {
            None
        } else {
            Some(secret.to_string())
        }
    });
    Ok(RedisUrl {
        host,
        port,
        password,
        database,
    })
}

pub fn encode_command(args: &[&[u8]]) -> Vec<u8> {
    let mut out = format!("*{}\r\n", args.len()).into_bytes();
    for arg in args {
        out.extend_from_slice(format!("${}\r\n", arg.len()).as_bytes());
        out.extend_from_slice(arg);
        out.extend_from_slice(b"\r\n");
    }
    out
}

struct DecodedFrame {
    value: Result<RedisValue, String>,
    used: usize,
}

impl DecodedFrame {
    fn value(value: RedisValue, used: usize) -> Self {
        Self {
            value: Ok(value),
            used,
        }
    }
}

pub fn decode(input: &[u8]) -> Result<Option<(RedisValue, usize)>, String> {
    match decode_frame(input)? {
        Some(frame) => frame.value.map(|value| Some((value, frame.used))),
        None => Ok(None),
    }
}

fn decode_frame(input: &[u8]) -> Result<Option<DecodedFrame>, String> {
    if input.is_empty() {
        return Ok(None);
    }
    match input[0] {
        b'+' => line(input, 1).map(|item| {
            item.map(|(text, used)| DecodedFrame::value(RedisValue::Status(text), used))
        }),
        b'-' => match line(input, 1)? {
            None => Ok(None),
            Some((text, used)) => Ok(Some(DecodedFrame {
                value: Err(text),
                used,
            })),
        },
        b':' => match line(input, 1)? {
            None => Ok(None),
            Some((text, used)) => Ok(Some(DecodedFrame::value(
                RedisValue::Int(
                    text.parse()
                        .map_err(|_| "invalid redis integer".to_string())?,
                ),
                used,
            ))),
        },
        b'$' => bulk(input),
        b'*' => array(input),
        other => Err(format!("invalid redis prefix {}", other as char)),
    }
}

fn line(input: &[u8], start: usize) -> Result<Option<(String, usize)>, String> {
    let Some(end) = find_crlf(&input[start..]) else {
        return Ok(None);
    };
    let text = std::str::from_utf8(&input[start..start + end])
        .map_err(|_| "redis reply is not utf-8".to_string())?;
    Ok(Some((text.to_string(), start + end + 2)))
}

fn find_crlf(input: &[u8]) -> Option<usize> {
    input.windows(2).position(|pair| pair == b"\r\n")
}

fn bulk(input: &[u8]) -> Result<Option<DecodedFrame>, String> {
    let Some((header, header_used)) = line(input, 1)? else {
        return Ok(None);
    };
    let length: i64 = header
        .parse()
        .map_err(|_| "invalid bulk length".to_string())?;
    if length < 0 {
        return Ok(Some(DecodedFrame::value(RedisValue::Nil, header_used)));
    }
    let length = length as usize;
    if input.len() < header_used + length + 2 {
        return Ok(None);
    }
    let bytes = &input[header_used..header_used + length];
    let text = std::str::from_utf8(bytes).map_err(|_| "redis bulk is not utf-8".to_string())?;
    Ok(Some(DecodedFrame::value(
        RedisValue::Bulk(text.to_string()),
        header_used + length + 2,
    )))
}

fn array(input: &[u8]) -> Result<Option<DecodedFrame>, String> {
    let Some((header, mut used)) = line(input, 1)? else {
        return Ok(None);
    };
    let length: i64 = header
        .parse()
        .map_err(|_| "invalid array length".to_string())?;
    if length < 0 {
        return Ok(Some(DecodedFrame::value(RedisValue::Nil, used)));
    }
    let mut items = Vec::new();
    let mut server_error = None;
    for _ in 0..length {
        match decode_frame(&input[used..])? {
            None => return Ok(None),
            Some(frame) => {
                used += frame.used;
                match frame.value {
                    Ok(value) => items.push(value),
                    Err(failure) if server_error.is_none() => server_error = Some(failure),
                    Err(_) => {}
                }
            }
        }
    }
    Ok(Some(DecodedFrame {
        value: server_error.map_or_else(|| Ok(RedisValue::Array(items)), Err),
        used,
    }))
}

pub struct BlockingRedisClient {
    stream: StdTcpStream,
    buffer: Vec<u8>,
}

impl BlockingRedisClient {
    pub fn connect(url: &str) -> Result<Self, String> {
        let parsed = parse_redis_url(url)?;
        let stream = StdTcpStream::connect((parsed.host.as_str(), parsed.port))
            .map_err(|error| error.to_string())?;
        stream
            .set_nodelay(true)
            .map_err(|error| error.to_string())?;
        let timeout = Some(Duration::from_secs(30));
        stream
            .set_read_timeout(timeout)
            .map_err(|error| error.to_string())?;
        stream
            .set_write_timeout(timeout)
            .map_err(|error| error.to_string())?;
        let mut client = Self {
            stream,
            buffer: Vec::new(),
        };
        if let Some(password) = parsed.password {
            client.call(&[b"AUTH", password.as_bytes()])?;
        }
        if parsed.database != 0 {
            let database = parsed.database.to_string();
            client.call(&[b"SELECT", database.as_bytes()])?;
        }
        Ok(client)
    }

    pub fn call(&mut self, args: &[&[u8]]) -> Result<RedisValue, String> {
        self.stream
            .write_all(&encode_command(args))
            .map_err(|error| error.to_string())?;
        self.stream.flush().map_err(|error| error.to_string())?;
        loop {
            match decode_frame(&self.buffer)? {
                Some(frame) => {
                    self.buffer.drain(..frame.used);
                    return frame.value;
                }
                None => {
                    let mut chunk = [0u8; 8192];
                    let read = self
                        .stream
                        .read(&mut chunk)
                        .map_err(|error| error.to_string())?;
                    if read == 0 {
                        return Err("redis connection closed".to_string());
                    }
                    self.buffer.extend_from_slice(&chunk[..read]);
                }
            }
        }
    }
}

pub fn bulk_string(value: &RedisValue) -> Result<Option<String>, String> {
    match value {
        RedisValue::Nil => Ok(None),
        RedisValue::Bulk(text) => Ok(Some(text.clone())),
        RedisValue::Status(text) => Ok(Some(text.clone())),
        other => Err(format!("expected redis bulk string, got {other:?}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn error_then_success_server() -> (String, std::thread::JoinHandle<()>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("redis://{}", listener.local_addr().unwrap());
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let request = encode_command(&[b"PING"]);
            let mut received = vec![0; request.len()];
            for reply in [
                b"-ERR BACKUP_FENCED\r\n".as_slice(),
                b"+PONG\r\n".as_slice(),
            ] {
                std::io::Read::read_exact(&mut stream, &mut received).unwrap();
                assert_eq!(received, request);
                std::io::Write::write_all(&mut stream, reply).unwrap();
            }
        });
        (url, server)
    }

    #[test]
    fn blocking_connection_recovers_after_a_redis_error_reply() {
        let (url, server) = error_then_success_server();
        let mut client = BlockingRedisClient::connect(&url).unwrap();
        assert_eq!(client.call(&[b"PING"]).unwrap_err(), "ERR BACKUP_FENCED");
        assert_eq!(
            client.call(&[b"PING"]).unwrap(),
            RedisValue::Status("PONG".into())
        );
        server.join().unwrap();
    }

    #[tokio::test]
    async fn asynchronous_connection_recovers_after_a_redis_error_reply() {
        let (url, server) = error_then_success_server();
        let mut client = RedisClient::connect(&url).await.unwrap();
        assert_eq!(
            client.call(&[b"PING"]).await.unwrap_err(),
            "ERR BACKUP_FENCED"
        );
        assert_eq!(
            client.call(&[b"PING"]).await.unwrap(),
            RedisValue::Status("PONG".into())
        );
        server.join().unwrap();
    }

    #[test]
    fn an_array_error_consumes_its_whole_frame_and_preserves_the_next_reply() {
        let failed = b"*2\r\n-ERR BACKUP_FENCED\r\n+OK\r\n";
        assert!(decode_frame(&failed[..failed.len() - 1]).unwrap().is_none());
        let bytes = [failed.as_slice(), b"+PONG\r\n".as_slice()].concat();
        let frame = decode_frame(&bytes).unwrap().unwrap();
        assert_eq!(frame.value.unwrap_err(), "ERR BACKUP_FENCED");
        assert_eq!(frame.used, failed.len());
        assert_eq!(
            decode(&bytes[frame.used..]).unwrap().unwrap().0,
            RedisValue::Status("PONG".into())
        );
    }

    #[test]
    fn encodes_and_decodes_replies() {
        assert_eq!(encode_command(&[b"PING"]), b"*1\r\n$4\r\nPING\r\n");
        assert_eq!(
            decode(b"+PONG\r\n").unwrap().unwrap().0,
            RedisValue::Status("PONG".to_string())
        );
        assert_eq!(decode(b"$-1\r\n").unwrap().unwrap().0, RedisValue::Nil);
        assert_eq!(decode(b":1\r\n").unwrap().unwrap().0, RedisValue::Int(1));
        let (value, _) = decode(b"*2\r\n$7\r\ncreated\r\n$5\r\nhello\r\n")
            .unwrap()
            .unwrap();
        assert_eq!(
            value,
            RedisValue::Array(vec![
                RedisValue::Bulk("created".to_string()),
                RedisValue::Bulk("hello".to_string())
            ])
        );
    }

    #[test]
    fn parses_authenticated_redis_url() {
        let parsed = parse_redis_url("redis://:secret@127.0.0.1:6379/2").unwrap();
        assert_eq!(parsed.host, "127.0.0.1");
        assert_eq!(parsed.port, 6379);
        assert_eq!(parsed.password.as_deref(), Some("secret"));
        assert_eq!(parsed.database, 2);
    }

    #[tokio::test]
    async fn ping_round_trip_against_a_local_stub() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 64];
            let _ = socket.read(&mut buf).await.unwrap();
            socket.write_all(b"+PONG\r\n").await.unwrap();
        });
        let mut client = RedisClient::connect(&format!("redis://{}", address))
            .await
            .unwrap();
        // connect() does not ping. The stub already consumed nothing until call.
        // The spawned server reads one command. connect() sends nothing when there is no password.
        client.ping().await.unwrap();
        server.await.unwrap();
    }
}
