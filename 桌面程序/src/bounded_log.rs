use std::{fs::{File, OpenOptions}, io::{self, Read, Seek, SeekFrom, Write}, path::Path, sync::{Arc, Mutex}, thread};

const LIMIT: u64 = 2 * 1024 * 1024;

pub struct BoundedLog { file: File, size: u64 }

impl BoundedLog {
    pub fn open(path: &Path) -> io::Result<Self> {
        // Windows 的 append-only 句柄无法 set_len；由唯一后台写入者控制位置。
        let mut file = OpenOptions::new().create(true).write(true).read(true).open(path)?;
        let size = file.metadata()?.len();
        file.seek(SeekFrom::End(0))?;
        let mut log = Self { file, size };
        if size > LIMIT { log.reset()?; }
        Ok(log)
    }

    fn reset(&mut self) -> io::Result<()> {
        self.file.set_len(0)?;
        self.file.seek(SeekFrom::Start(0))?;
        self.size = 0;
        Ok(())
    }

    pub fn write(&mut self, data: &[u8]) -> io::Result<()> {
        let tail = &data[data.len().saturating_sub(LIMIT as usize)..];
        if self.size + tail.len() as u64 > LIMIT { self.reset()?; }
        self.file.write_all(tail)?;
        self.size += tail.len() as u64;
        Ok(())
    }
}

pub fn capture(stream: impl Read + Send + 'static, log: Arc<Mutex<BoundedLog>>) {
    thread::spawn(move || {
        let mut stream = stream;
        let mut bytes = [0u8; 8192];
        loop {
            match stream.read(&mut bytes) {
                Ok(0) | Err(_) => break,
                Ok(count) => { if let Ok(mut output) = log.lock() { let _ = output.write(&bytes[..count]); } }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn continuous_output_keeps_only_bounded_recent_diagnostics() {
        let path = std::env::temp_dir().join(format!("framekeep-log-{}",uuid::Uuid::new_v4()));
        let mut log = BoundedLog::open(&path).unwrap();
        log.write(&vec![b'a'; LIMIT as usize]).unwrap();
        log.write(b"recent failure").unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), b"recent failure");
        log.write(&vec![b'b'; LIMIT as usize + 10]).unwrap();
        assert_eq!(std::fs::metadata(&path).unwrap().len(), LIMIT);
        drop(log);
        std::fs::remove_file(path).unwrap();
    }
}
