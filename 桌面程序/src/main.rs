#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    fs::{self, OpenOptions},
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::{Manager, WebviewUrl, WebviewWindow, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

struct Engine {
    child: Mutex<Option<Child>>,
    port: u16,
    token: String,
    data: PathBuf,
    app_root: PathBuf,
    ready: AtomicBool,
    closing: AtomicBool,
    started: Instant,
}

#[tauri::command]
fn desktop_ready(engine: tauri::State<'_, Arc<Engine>>) {
    let _ = fs::write(engine.data.join("desktop-metrics.json"), serde_json::json!({"first_document_ready_ms":engine.started.elapsed().as_millis(),"desktop_pid":std::process::id()}).to_string());
    if engine.ready.load(Ordering::SeqCst) {
        if let (Some(file), Ok(config)) = (std::env::var_os("FRAMEKEEP_UPDATE_HEALTH_FILE"), fs::read_to_string(engine.app_root.join("update-config.json"))) {
            if let Ok(identity) = serde_json::from_str::<serde_json::Value>(&config) {
                let target=PathBuf::from(file);
                let temporary=target.with_extension("tmp");
                if fs::write(&temporary, serde_json::json!({"pid":std::process::id(),"version":identity["version"],"releaseId":identity["releaseId"]}).to_string()).is_ok() {
                    let _ = fs::rename(temporary,target);
                }
            }
        }
    }
}

#[tauri::command]
fn close_for_update(window: WebviewWindow, engine: tauri::State<'_, Arc<Engine>>) -> Result<(), String> {
    let health = engine.request("/api/update/health", false)?;
    if health["busy"].as_bool().unwrap_or(true) { return Err("仍有后台任务，请完成或停止后重试更新".into()); }
    engine.closing.store(true, Ordering::SeqCst);
    window.app_handle().exit(0);
    Ok(())
}

fn data_directory(app_root: &Path, explicit: Option<PathBuf>) -> PathBuf {
    if let Some(path) = explicit {
        return path;
    }
    if let Ok(text) = fs::read_to_string(app_root.join(".framekeep-install.json")) {
        if let Ok(state) = serde_json::from_str::<serde_json::Value>(&text) {
            if state["product"] == "framekeep"
                && state["root"].as_str().map(|p| p.eq_ignore_ascii_case(&app_root.to_string_lossy())).unwrap_or(false)
            {
                if let Some(path) = state["dataPath"].as_str().map(PathBuf::from) {
                    if path.is_absolute() { return path; }
                }
            }
        }
    }
    let legacy=app_root.join(".framekeep");
    if legacy.is_dir() && !app_root.join("data").is_dir() { return legacy; }
    app_root.join("data")
}

impl Engine {
    fn deliver_photos(&self) {
        let inbox=self.data.join("canon-link/inbox");
        while !self.closing.load(Ordering::SeqCst) {
            let mut pending=fs::read_dir(&inbox).ok().into_iter().flatten().filter_map(Result::ok)
                .map(|item|item.path()).filter(|path|path.extension().and_then(|s|s.to_str())==Some("json")).collect::<Vec<_>>();
            pending.sort();
            for path in pending {
                if self.closing.load(Ordering::SeqCst) {return;}
                if let Ok(payload)=fs::read_to_string(&path) {
                    match self.post_json("/api/desktop/canon",&payload) {
                        Ok(result)=>{
                            if result.get("error").is_some() {let _=fs::rename(&path,path.with_extension("rejected"));}
                            else {let _=fs::remove_file(&path);}
                        }
                        Err(_)=>break,
                    }
                }
            }
            thread::sleep(Duration::from_millis(80));
        }
    }

    fn post_json(&self,path:&str,body:&str)->Result<serde_json::Value,String> {
        let mut stream=TcpStream::connect_timeout(&SocketAddr::from(([127,0,0,1],self.port)),Duration::from_millis(400)).map_err(|e|e.to_string())?;
        stream.set_read_timeout(Some(Duration::from_secs(8))).map_err(|e|e.to_string())?;
        stream.set_write_timeout(Some(Duration::from_secs(2))).map_err(|e|e.to_string())?;
        let request=format!("POST {path} HTTP/1.0\r\nHost: 127.0.0.1:{}\r\nX-Framekeep-Desktop: {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",self.port,self.token,body.len(),body);
        stream.write_all(request.as_bytes()).map_err(|e|e.to_string())?;
        let mut response=Vec::new();stream.read_to_end(&mut response).map_err(|e|e.to_string())?;
        let start=response.windows(4).position(|w|w==b"\r\n\r\n").ok_or("照片接入服务未返回响应")?+4;
        serde_json::from_slice(&response[start..]).map_err(|e|e.to_string())
    }

    fn request(&self, path: &str, shutdown: bool) -> Result<serde_json::Value, String> {
        let addr = SocketAddr::from(([127, 0, 0, 1], self.port));
        let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(400))
            .map_err(|e| e.to_string())?;
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .map_err(|e| e.to_string())?;
        let request = if shutdown {
            format!("POST {path} HTTP/1.0\r\nHost: 127.0.0.1:{}\r\nX-Framekeep-Desktop: {}\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{{}}",self.port,self.token)
        } else {
            format!(
                "GET {path} HTTP/1.0\r\nHost: 127.0.0.1:{}\r\n\r\n",
                self.port
            )
        };
        stream
            .write_all(request.as_bytes())
            .map_err(|e| e.to_string())?;
        let mut response = Vec::new();
        stream
            .read_to_end(&mut response)
            .map_err(|e| e.to_string())?;
        let body = response
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .ok_or("本机引擎未返回有效响应")?
            + 4;
        serde_json::from_slice(&response[body..]).map_err(|e| e.to_string())
    }
    fn start(&self) -> Result<(), String> {
        if self.app_root.join(".update-state/pending.json").is_file()
            && std::env::var_os("FRAMEKEEP_UPDATE_HEALTH_FILE").is_none() {
            let mut recovery = Command::new(self.app_root.join("update/update-worker.exe"));
            recovery.arg(&self.app_root).arg("--recover");
            #[cfg(windows)]
            { use std::os::windows::process::CommandExt; recovery.creation_flags(0x08000000); }
            let output = recovery.output().map_err(|e| format!("无法恢复中断的更新：{e}"))?;
            if !output.status.success() { return Err("上次更新恢复失败，请运行轻量安装器修复；保留原目录及更新备份。".into()); }
        }
        if TcpStream::connect_timeout(
            &SocketAddr::from(([127, 0, 0, 1], self.port)),
            Duration::from_millis(200),
        )
        .is_ok()
        {
            return Err(format!(
                "端口 {} 已被使用。请关闭旧版选片后台程序，再启动桌面版。",
                self.port
            ));
        }
        fs::create_dir_all(&self.data).map_err(|e| e.to_string())?;
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.data.join("engine.log"))
            .map_err(|e| e.to_string())?;
        let engine = std::env::var_os("FRAMEKEEP_ENGINE")
            .map(PathBuf::from)
            .unwrap_or_else(|| self.app_root.join("engine/framekeep-engine.exe"));
        let mut command = Command::new(engine);
        command
            .args(["--port", &self.port.to_string(), "--no-browser"])
            .env("FRAMEKEEP_DATA", &self.data)
            .env("FRAMEKEEP_APP_ROOT", &self.app_root)
            .env("FRAMEKEEP_DESKTOP_TOKEN", &self.token)
            .env("FRAMEKEEP_DESKTOP_PID", std::process::id().to_string())
            .env("FRAMEKEEP_DESKTOP_EXE",std::env::current_exe().map_err(|e|e.to_string())?)
            .stdin(Stdio::null())
            .stdout(log.try_clone().map_err(|e| e.to_string())?)
            .stderr(log);
        if std::env::var_os("FRAMEKEEP_CONFIG").is_none()
            && !self.data.join("config-location").is_file()
        {
            if let Some(project) = self.data.parent() {
                let config = project.join("api key.md");
                if config.is_file() {
                    command.env("FRAMEKEEP_CONFIG", config);
                }
            }
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let pid = {
            let mut slot = self.child.lock().unwrap();
            if self.closing.load(Ordering::SeqCst) {
                return Err("程序正在关闭".into());
            }
            let child = command
                .spawn()
                .map_err(|e| format!("无法启动本机选片引擎：{e}"))?;
            let pid = child.id();
            *slot = Some(child);
            pid
        };
        let started = Instant::now();
        while started.elapsed() < Duration::from_secs(30) {
            if let Ok(health) = self.request("/api/health", false) {
                if health["process_id"].as_u64() == Some(pid as u64)
                    && health["application"] == "framekeep"
                {
                    self.ready.store(true, Ordering::SeqCst);
                    return Ok(());
                }
            }
            if let Some(child) = self.child.lock().unwrap().as_mut() {
                if let Some(code) = child.try_wait().map_err(|e| e.to_string())? {
                    return Err(format!(
                        "选片引擎提前退出（{code}），请检查数据目录中的 engine.log。"
                    ));
                }
            }
            thread::sleep(Duration::from_millis(80));
        }
        Err("选片引擎启动超时，请检查数据目录中的 engine.log。".into())
    }
    fn stop(&self) {
        self.closing.store(true, Ordering::SeqCst);
        let mut child = self.child.lock().unwrap();
        if let Some(process) = child.as_mut() {
            let _ = self.request("/api/desktop/shutdown", true);
            let until = Instant::now() + Duration::from_secs(4);
            while Instant::now() < until {
                if process.try_wait().ok().flatten().is_some() {
                    *child = None;
                    return;
                }
                thread::sleep(Duration::from_millis(80));
            }
            let _ = process.kill();
            let _ = process.wait();
            *child = None;
        }
    }
}

#[tauri::command]
async fn choose_folders(window: WebviewWindow) -> Result<Vec<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        window
            .dialog()
            .file()
            .set_title("添加照片文件夹")
            .set_parent(&window)
            .blocking_pick_folders()
            .unwrap_or_default()
            .into_iter()
            .map(|p| {
                p.into_path()
                    .map(|p| p.to_string_lossy().into_owned())
                    .map_err(|e| e.to_string())
            })
            .collect()
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn choose_cache_folder(window: WebviewWindow) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        window.dialog().file().set_title("选择缓存或备份文件夹").set_parent(&window)
            .blocking_pick_folder()
            .map(|p| p.into_path().map(|p| p.to_string_lossy().into_owned()).map_err(|e| e.to_string())).transpose()
    }).await.map_err(|e| e.to_string())?
}

#[tauri::command]
async fn choose_cache_backup(window: WebviewWindow) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        window.dialog().file().set_title("导入图片缓存备份").set_parent(&window)
            .add_filter("末帧缓存备份", &["zip"]).blocking_pick_file()
            .map(|p| p.into_path().map(|p| p.to_string_lossy().into_owned()).map_err(|e| e.to_string())).transpose()
    }).await.map_err(|e| e.to_string())?
}

#[tauri::command]
async fn save_manifest(
    window: WebviewWindow,
    suggested_name: String,
    data: serde_json::Value,
) -> Result<bool, String> {
    let metadata = data["schema"] == "framekeep.sku-metadata" && data["version"] == 1;
    if !metadata && (data["schema"] != "framekeep.selection" || data["demo"] != false) {
        return Err("只能保存正式选片清单或 SKU 元数据备份。".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        let name = Path::new(&suggested_name)
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("选片结果.framekeep.json");
        let Some(path) = window
            .dialog()
            .file()
            .set_title(if metadata { "导出 SKU 元数据" } else { "保存选片结果" })
            .set_parent(&window)
            .set_file_name(name)
            .add_filter(if metadata { "SKU 元数据" } else { "选片清单" }, &["json"])
            .blocking_save_file()
        else {
            return Ok(false);
        };
        let path = path.into_path().map_err(|e| e.to_string())?;
        let bytes = serde_json::to_vec_pretty(&data).map_err(|e| e.to_string())?;
        fs::write(path, bytes).map_err(|e| e.to_string())?;
        Ok(true)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn save_sku_workbook(
    window: WebviewWindow,
    engine: tauri::State<'_, Arc<Engine>>,
    project_id: String,
    suggested_name: String,
) -> Result<bool, String> {
    if project_id.len() != 32 || !project_id.bytes().all(|c| c.is_ascii_hexdigit()) {
        return Err("对账任务标识无效".into());
    }
    let source = engine.data.join("sku-projects").join(project_id).join("拍摄对账.xlsx");
    tauri::async_runtime::spawn_blocking(move || {
        if !source.is_file() { return Err("对账 Excel 尚未生成，请重试导出".into()); }
        let name = Path::new(&suggested_name).file_name().and_then(|s|s.to_str()).unwrap_or("拍摄对账.xlsx");
        let Some(path) = window.dialog().file().set_title("保存拍摄对账 Excel").set_parent(&window).set_file_name(name).add_filter("Excel", &["xlsx"]).blocking_save_file() else { return Ok(false); };
        let target = path.into_path().map_err(|e|e.to_string())?;
        if target == source { return Ok(true); }
        fs::copy(source,target).map_err(|e|e.to_string())?;
        Ok(true)
    }).await.map_err(|e|e.to_string())?
}

#[tauri::command]
async fn finish_close(
    window: WebviewWindow,
    engine: tauri::State<'_, Arc<Engine>>,
) -> Result<bool, String> {
    let engine = engine.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        if let Ok(health) = engine.request("/api/health", false) {
            if health["busy"].as_bool().unwrap_or(false)
                && !window
                    .dialog()
                    .message("仍有索引、缓存管理或 LR 同步任务。关闭程序将中断未完成的任务，是否退出？")
                    .title("退出末帧选片")
                    .buttons(MessageDialogButtons::OkCancel)
                    .blocking_show()
            {
                return Ok(false);
            }
        }
        engine.closing.store(true, Ordering::SeqCst);
        window.app_handle().exit(0);
        Ok(true)
    })
    .await
    .map_err(|e| e.to_string())?
}

fn photo_arguments(args:&[String],cwd:&Path)->Vec<String> {
    args.iter().skip(1).filter_map(|value|{
        let path=Path::new(value);
        let extension=path.extension()?.to_str()?.to_ascii_lowercase();
        if !["jpg","jpeg","tif","tiff","png","heic","heif","cr3","cr2","nef","nrw","arw","srf","sr2","raf","rw2","orf","dng","pef","3fr","fff","rwl","iiq","srw"].contains(&extension.as_str()) {return None;}
        Some(if path.is_absolute(){path.to_path_buf()}else{cwd.join(path)}.to_string_lossy().to_string())
    }).take(64).collect()
}

fn queue_photos(data:&Path,args:&[String],cwd:&Path)->bool {
    let paths=photo_arguments(args,cwd);if paths.is_empty(){return false;}
    let inbox=data.join("canon-link/inbox");
    let stamp=SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos();
    let target=inbox.join(format!("{:032}-{}.json",stamp,uuid::Uuid::new_v4()));
    let temporary=target.with_extension("tmp");
    if fs::create_dir_all(&inbox).is_ok() {
        if let Ok(payload)=serde_json::to_vec(&serde_json::json!({"paths":paths})) {
            if fs::write(&temporary,payload).is_ok(){let _=fs::rename(&temporary,&target);}
        }
    }
    true
}

fn main() {
    let app_root = std::env::current_exe()
        .expect("无法获取程序位置")
        .parent()
        .unwrap()
        .to_path_buf();
    let data = data_directory(
        &app_root,
        std::env::var_os("FRAMEKEEP_DATA").map(PathBuf::from),
    );
    let port = std::env::var("FRAMEKEEP_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(8769);
    let initial_args=std::env::args().collect::<Vec<_>>();
    let initial_cwd=std::env::current_dir().unwrap_or_default();
    let inbox_data=data.clone();
    let engine = Arc::new(Engine {
        child: Mutex::new(None),
        port,
        token: uuid::Uuid::new_v4().to_string(),
        data,
        app_root,
        ready: AtomicBool::new(false),
        closing: AtomicBool::new(false),
        started: Instant::now(),
    });
    let cleanup = engine.clone();
    let app=tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(move |app,args,cwd|{
            if queue_photos(&inbox_data,&args,Path::new(&cwd)){return;}
            if let Some(w)=app.get_webview_window("main"){let _=w.unminimize();let _=w.show();let _=w.set_focus();}
        }))
        .plugin(tauri_plugin_dialog::init()).manage(engine.clone())
        .invoke_handler(tauri::generate_handler![choose_folders,choose_cache_folder,choose_cache_backup,save_manifest,save_sku_workbook,finish_close,desktop_ready,close_for_update])
        .setup(move |app| {
            queue_photos(&engine.data,&initial_args,&initial_cwd);
            let window=tauri::WebviewWindowBuilder::new(app,"main",WebviewUrl::App("index.html".into()))
                .title("末帧选片").inner_size(1440.,900.).min_inner_size(1000.,680.).center()
                // 让资源管理器文件拖放进入前端的HTML5事件，避免WebView2被原生处理器截获。
                .disable_drag_drop_handler()
                .on_navigation(move |url|url.scheme()=="tauri"||url.host_str()==Some("tauri.localhost")||(url.scheme()=="http"&&url.host_str()==Some("127.0.0.1")&&url.port_or_known_default()==Some(port)))
                .build()?;
            let events=engine.clone();let close_window=window.clone();
            window.on_window_event(move |event| {
                if let WindowEvent::CloseRequested{api,..}=event {
                    if events.ready.load(Ordering::SeqCst)&&!events.closing.load(Ordering::SeqCst) {
                        api.prevent_close();let _=close_window.eval("if(window.FramekeepDesktop){window.FramekeepDesktop.close()}else{window.__TAURI__.core.invoke('finish_close')}");
                    }
                }
            });
            let handle=app.handle().clone();
            thread::spawn(move || {
                match engine.start() {
                    Ok(())=>{
                        let delivery=engine.clone();thread::spawn(move||delivery.deliver_photos());
                        let _=window.navigate(format!("http://127.0.0.1:{}/",engine.port).parse().unwrap());
                    }
                    Err(error)=>{handle.dialog().message(error).title("无法打开末帧选片").blocking_show();handle.exit(1);}
                }
            });
            Ok(())
        }).build(tauri::generate_context!()).expect("无法创建桌面窗口");
    app.run(move |_, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            cleanup.stop();
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn canon_arguments_keep_unicode_paths_and_ignore_non_images() {
        let cwd=Path::new("C:/拍摄 目录");
        let args=vec!["末帧.exe".into(),"第一张.JPG".into(),"--unknown".into(),"data.txt".into(),"C:/RAW/001.CR3".into()];
        let paths=photo_arguments(&args,cwd);
        assert_eq!(paths.len(),2);assert_eq!(PathBuf::from(&paths[0]),cwd.join("第一张.JPG"));
        assert_eq!(PathBuf::from(&paths[1]),PathBuf::from("C:/RAW/001.CR3"));
    }
    #[test]
    fn canon_inbox_is_persistent_and_limits_notifications() {
        let data=std::env::temp_dir().join(format!("canon-inbox-{}",uuid::Uuid::new_v4()));
        let args=vec!["app.exe".into(),"photo.JPG".into()];
        assert!(queue_photos(&data,&args,Path::new("C:/shots")));
        let files=fs::read_dir(data.join("canon-link/inbox")).unwrap().map(Result::unwrap).collect::<Vec<_>>();
        assert_eq!(files.len(),1);
        let payload:serde_json::Value=serde_json::from_slice(&fs::read(files[0].path()).unwrap()).unwrap();
        assert_eq!(PathBuf::from(payload["paths"][0].as_str().unwrap()),Path::new("C:/shots").join("photo.JPG"));
        fs::remove_file(files[0].path()).unwrap();fs::remove_dir(data.join("canon-link/inbox")).unwrap();
        fs::remove_dir(data.join("canon-link")).unwrap();fs::remove_dir(data).unwrap();
    }
    #[test]
    fn explicit_data_wins() {
        assert_eq!(
            data_directory(Path::new("C:/portable"), Some(PathBuf::from("D:/ratings"))),
            PathBuf::from("D:/ratings")
        );
    }
    #[test]
    fn portable_data_stays_beside_program() {
        assert_eq!(
            data_directory(Path::new("C:/portable"), None),
            PathBuf::from("C:/portable/data")
        );
    }
    #[test]
    fn managed_install_uses_its_user_data_marker() {
        let root=std::env::temp_dir().join(format!("framekeep-install-{}",uuid::Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let data=root.join("user-data");
        fs::write(root.join(".framekeep-install.json"),serde_json::json!({"product":"framekeep","root":root,"dataPath":data}).to_string()).unwrap();
        assert_eq!(data_directory(&root,None),data);
        fs::remove_file(root.join(".framekeep-install.json")).unwrap();fs::remove_dir(root).unwrap();
    }
}
