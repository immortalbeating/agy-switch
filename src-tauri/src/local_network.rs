use serde::{Deserialize, Serialize};
use std::fs::{File, OpenOptions, TryLockError};
use std::io;
use std::net::{Ipv4Addr, TcpListener};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// Keep the bound listener: checking a port then rebinding introduces a race.
pub fn bind_loopback(ports: impl IntoIterator<Item = u16>) -> io::Result<TcpListener> {
    bind_loopback_with(ports, |port| TcpListener::bind((Ipv4Addr::LOCALHOST, port)))
}

fn bind_loopback_with(
    ports: impl IntoIterator<Item = u16>,
    mut bind: impl FnMut(u16) -> io::Result<TcpListener>,
) -> io::Result<TcpListener> {
    for port in ports {
        if let Ok(listener) = bind(port) {
            return Ok(listener);
        }
    }
    // Port 0 lets Windows avoid both occupied and excluded port ranges.
    bind(0)
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct InstanceInfo {
    pub port: u16,
    pub instance_id: String,
}

/// The OS releases the lock even after a crash; stale metadata cannot block startup.
pub struct InstanceGuard {
    _lock: File,
    info_path: PathBuf,
}

impl InstanceGuard {
    pub fn try_acquire(dir: &Path) -> io::Result<Option<Self>> {
        std::fs::create_dir_all(dir)?;
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(dir.join("instance.lock"))?;
        match lock.try_lock() {
            Ok(()) => Ok(Some(Self { _lock: lock, info_path: dir.join("instance.json") })),
            Err(TryLockError::WouldBlock) => Ok(None),
            Err(TryLockError::Error(e)) => Err(e),
        }
    }

    pub fn publish(&self, info: &InstanceInfo) -> io::Result<()> {
        // Readers retry while the first process is starting, including partial writes.
        std::fs::write(&self.info_path, serde_json::to_vec(info)?)
    }
}

impl Drop for InstanceGuard {
    fn drop(&mut self) {
        // Remove metadata while we still own the lock.
        let _ = std::fs::remove_file(&self.info_path);
    }
}

pub fn loopback_client() -> reqwest::Result<reqwest::blocking::Client> {
    reqwest::blocking::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(1))
        .build()
}

fn try_wake(client: &reqwest::blocking::Client, info: &InstanceInfo) -> bool {
    if info.port == 0 || info.instance_id.is_empty() {
        return false;
    }
    let base = format!("http://127.0.0.1:{}", info.port);
    let matches = client.get(format!("{base}/api/instance"))
        .send().and_then(|r| r.error_for_status())
        .ok().and_then(|r| r.json::<serde_json::Value>().ok())
        .is_some_and(|v| v["app"] == "agy-switch" && v["instance_id"] == info.instance_id);
    matches && client.post(format!("{base}/api/show"))
        .send().and_then(|r| r.error_for_status()).is_ok()
}

pub fn wake_existing_instance(dir: &Path, timeout: Duration) -> bool {
    let Ok(client) = loopback_client() else { return false };
    let deadline = Instant::now() + timeout;
    loop {
        let info = std::fs::read(dir.join("instance.json")).ok()
            .and_then(|data| serde_json::from_slice::<InstanceInfo>(&data).ok());
        if info.is_some_and(|info| try_wake(&client, &info)) {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tiny_http::{Response, Server};

    struct TempDir(PathBuf);
    impl TempDir {
        fn new() -> Self {
            let dir = std::env::temp_dir().join(format!("agy-switch-network-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir_all(&dir).unwrap();
            Self(dir)
        }
    }
    impl Drop for TempDir {
        fn drop(&mut self) { let _ = std::fs::remove_dir_all(&self.0); }
    }

    #[test]
    fn excluded_ports_fall_back_to_a_real_listener() {
        let mut attempts = Vec::new();
        let listener = bind_loopback_with(8791..=8800, |port| {
            attempts.push(port);
            if port != 0 { Err(io::Error::from_raw_os_error(10013)) }
            else { TcpListener::bind((Ipv4Addr::LOCALHOST, 0)) }
        }).unwrap();
        assert_eq!(attempts, (8791..=8800).chain([0]).collect::<Vec<_>>());
        assert!(listener.local_addr().unwrap().ip().is_loopback());
        assert_ne!(listener.local_addr().unwrap().port(), 0);
    }

    #[test]
    fn occupied_port_falls_back_without_releasing_the_listener() {
        let occupied = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let port = occupied.local_addr().unwrap().port();
        let listener = bind_loopback([port]).unwrap();
        assert_ne!(listener.local_addr().unwrap().port(), port);
        let server = Server::from_listener(listener, None).unwrap();
        let address = server.server_addr().to_ip().unwrap();
        let worker = std::thread::spawn(move || {
            let request = server.recv_timeout(Duration::from_secs(3)).unwrap().unwrap();
            request.respond(Response::from_string("ready")).unwrap();
        });
        let response = loopback_client().unwrap().get(format!("http://{address}/")).send().unwrap();
        assert_eq!(response.text().unwrap(), "ready");
        worker.join().unwrap();
    }

    #[test]
    fn oauth_listener_uses_actual_port_under_current_windows_exclusions() {
        let listener = bind_loopback(8792..=8801).unwrap();
        let address = listener.local_addr().unwrap();
        assert_ne!(address.port(), 0);
        let client = std::net::TcpStream::connect(address).unwrap();
        let (_, remote) = listener.accept().unwrap();
        assert_eq!(client.local_addr().unwrap(), remote);
    }

    #[test]
    fn instance_lock_excludes_second_owner_and_releases_on_drop() {
        let dir = TempDir::new();
        let guard = InstanceGuard::try_acquire(&dir.0).unwrap().unwrap();
        guard.publish(&InstanceInfo { port: 18791, instance_id: "test".into() }).unwrap();
        assert!(InstanceGuard::try_acquire(&dir.0).unwrap().is_none());
        drop(guard);
        assert!(!dir.0.join("instance.json").exists());
        assert!(InstanceGuard::try_acquire(&dir.0).unwrap().is_some());
    }

    #[test]
    fn dynamic_instance_is_identified_and_woken_on_its_actual_port() {
        let dir = TempDir::new();
        let guard = InstanceGuard::try_acquire(&dir.0).unwrap().unwrap();
        let server = Server::from_listener(bind_loopback([]).unwrap(), None).unwrap();
        let port = server.server_addr().to_ip().unwrap().port();
        guard.publish(&InstanceInfo { port, instance_id: "dynamic-instance".into() }).unwrap();
        let worker = std::thread::spawn(move || {
            let request = server.recv_timeout(Duration::from_secs(3)).unwrap().unwrap();
            assert_eq!(request.url(), "/api/instance");
            request.respond(Response::from_string(r#"{"app":"agy-switch","instance_id":"dynamic-instance"}"#)).unwrap();
            let request = server.recv_timeout(Duration::from_secs(3)).unwrap().unwrap();
            assert_eq!(request.method(), &tiny_http::Method::Post);
            assert_eq!(request.url(), "/api/show");
            request.respond(Response::from_string(r#"{"ok":true}"#)).unwrap();
        });
        assert!(wake_existing_instance(&dir.0, Duration::from_secs(2)));
        worker.join().unwrap();
    }

    #[test]
    fn stale_metadata_and_unrelated_server_cannot_be_woken() {
        let dir = TempDir::new();
        let guard = InstanceGuard::try_acquire(&dir.0).unwrap().unwrap();
        let server = Server::from_listener(bind_loopback([]).unwrap(), None).unwrap();
        let port = server.server_addr().to_ip().unwrap().port();
        guard.publish(&InstanceInfo { port, instance_id: "stale-instance".into() }).unwrap();
        let worker = std::thread::spawn(move || {
            let request = server.recv_timeout(Duration::from_secs(3)).unwrap().unwrap();
            request.respond(Response::from_string(r#"{"app":"agy-switch","instance_id":"other-instance"}"#)).unwrap();
            assert!(server.recv_timeout(Duration::from_millis(300)).unwrap().is_none());
        });
        assert!(!wake_existing_instance(&dir.0, Duration::ZERO));
        worker.join().unwrap();
        std::fs::write(dir.0.join("instance.json"), b"incomplete").unwrap();
        assert!(!wake_existing_instance(&dir.0, Duration::ZERO));
    }
}
