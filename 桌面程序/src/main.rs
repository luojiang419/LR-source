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
mod quick_search;
use quick_search::{search_show,search_hide,search_shortcut,search_open_asset};
mod desktop_preferences;
use desktop_preferences::{CloseBehavior, DesktopPreferences, Preferences, StartupMode};

#[tauri::command]
fn get_desktop_preferences(preferences: tauri::State<'_, DesktopPreferences>) -> Preferences {
    preferences.value.lock().unwrap().clone()
}

#[tauri::command]
fn set_desktop_preferences(preferences: tauri::State<'_, DesktopPreferences>, close_behavior: Option<CloseBehavior>, startup_mode: Option<StartupMode>) -> Result<Preferences, String> {
    let mut next = preferences.value.lock().unwrap().clone();
    if let Some(value) = close_behavior { next.close_behavior = value; }
    if let Some(value) = startup_mode { next.startup_mode = value; }
    preferences.save(next)
}

#[tauri::command]
fn hide_to_tray(window: WebviewWindow, preferences: tauri::State<'_, DesktopPreferences>, remember: bool) -> Result<(), String> {
    if window.app_handle().tray_by_id("main-tray").is_none() { return Err("托盘不可用，请选择退出程序".into()); }
    if remember {
        let mut next = preferences.value.lock().unwrap().clone();
        next.close_behavior = CloseBehavior::Tray;
        preferences.save(next)?;
    }
    window.hide().map_err(|e| e.to_string())
}

fn show_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize(); let _ = window.show(); let _ = window.set_focus();
    }
}

fn request_close(window: &WebviewWindow, force_exit: bool) {
    let action = if force_exit { "'exit'" } else { "null" };
    let _ = window.eval(format!("if(window.FramekeepDesktop){{window.FramekeepDesktop.close({action})}}else{{window.__framekeepPendingClose={action};window.__framekeepHasPendingClose=true}}"));
}

fn create_tray(app: &tauri::App) -> tauri::Result<()> {
    use tauri::{menu::{Menu, MenuItem}, tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent}};
    let show = MenuItem::with_id(app, "show", if desktop_preferences::development_build() { "打开末帧资产检索开发版" } else { "打开末帧选片" }, true, None::<&str>)?;
    let exit = MenuItem::with_id(app, "exit", "退出程序", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &exit])?;
    let mut tray = TrayIconBuilder::with_id("main-tray").tooltip(if desktop_preferences::development_build() { "末帧资产检索开发版 · 点击打开窗口" } else { "末帧选片 · 点击打开窗口" }).menu(&menu).show_menu_on_left_click(false);
    if let Some(icon) = app.default_window_icon() { tray = tray.icon(icon.clone()); }
    tray.on_menu_event(|app, event| match event.id.as_ref() {
        "show" => show_main(app),
        "exit" => { show_main(app); if let Some(window) = app.get_webview_window("main") { request_close(&window, true); } },
        _ => {}
    }).on_tray_icon_event(|tray, event| {
        if matches!(event, TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. }) { show_main(tray.app_handle()); }
    }).build(app)?;
    Ok(())
}

struct Engine {
    child: Mutex<Option<Child>>,
    port: u16,
    token: String,
    data: PathBuf,
    app_root: PathBuf,
    ready: AtomicBool,
    closing: AtomicBool,
    started: Instant,
    splash_visible: Mutex<Option<u128>>,
}

#[derive(Default)]
struct StartupWait {
    stage: String,
    last_progress: Duration,
}

impl StartupWait {
    fn observe(&mut self, status: &serde_json::Value, pid: u32, elapsed: Duration) {
        if status["process_id"].as_u64() != Some(pid as u64) { return; }
        if let Some(stage) = status["stage"].as_str() {
            if self.stage != stage {
                self.stage = stage.into();
                self.last_progress = elapsed;
            }
        }
    }

    fn expired(&self, elapsed: Duration) -> bool {
        elapsed >= Duration::from_secs(180)
            || elapsed.saturating_sub(self.last_progress) >= Duration::from_secs(60)
    }
}

fn startup_status_file(data: &Path) -> Option<serde_json::Value> {
    serde_json::from_str(&fs::read_to_string(data.join("engine-startup.json")).ok()?).ok()
}

#[tauri::command]
fn startup_status(engine: tauri::State<'_, Arc<Engine>>) -> serde_json::Value {
    startup_status_file(&engine.data)
        .filter(|status| status["desktop_pid"].as_str() == Some(&std::process::id().to_string()))
        .unwrap_or_else(|| serde_json::json!({"message":"正在启动选片服务…","stage":"initializing"}))
}

#[tauri::command]
fn startup_visible(engine: tauri::State<'_, Arc<Engine>>) {
    let mut visible=engine.splash_visible.lock().unwrap();
    if visible.is_none() {*visible=Some(engine.started.elapsed().as_millis());}
}

#[tauri::command]
fn desktop_ready(engine: tauri::State<'_, Arc<Engine>>, timings: Option<serde_json::Value>) {
    let elapsed=engine.started.elapsed().as_millis();
    let _ = fs::write(engine.data.join("desktop-metrics.json"), serde_json::json!({"first_document_ready_ms":elapsed,"first_workbench_ready_ms":elapsed,"startup_animation_visible_ms":*engine.splash_visible.lock().unwrap(),"web_timings":timings,"desktop_pid":std::process::id()}).to_string());
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
        let _ = fs::remove_file(self.data.join("engine-startup.json"));
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.data.join("engine.log"))
            .map_err(|e| e.to_string())?;
        let engine = std::env::var_os("FRAMEKEEP_ENGINE")
            .map(PathBuf::from)
            .unwrap_or_else(|| self.app_root.join("engine/framekeep-engine.exe"));
        let mut command = Command::new(engine);
        if let Some(script)=std::env::var_os("FRAMEKEEP_ENGINE_SCRIPT"){command.args(["-X","utf8"]).arg(script);}
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
        let mut waiting = StartupWait::default();
        let mut last_error = String::new();
        loop {
            if self.closing.load(Ordering::SeqCst) { return Err("程序正在关闭".into()); }
            if let Some(status) = startup_status_file(&self.data) {
                waiting.observe(&status, pid, started.elapsed());
            }
            if waiting.expired(started.elapsed()) { break; }
            match self.request("/api/ready", false) {
                Ok(health) => {
                if health["process_id"].as_u64() == Some(pid as u64)
                    && health["application"] == "framekeep"
                    && health["ready"] == true
                {
                    self.ready.store(true, Ordering::SeqCst);
                    return Ok(());
                }
                },
                Err(error) => last_error = error,
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
        if let Ok(mut log) = OpenOptions::new().create(true).append(true).open(self.data.join("engine.log")) {
            let _ = writeln!(log, "[desktop startup] engine_pid={pid} elapsed={:.3}s stage={} last_error={last_error}", started.elapsed().as_secs_f64(), waiting.stage);
        }
        Err(format!("选片引擎启动超时。\n请保留以下目录中的 engine.log、engine-startup.json 和 engine-startup-stacks.log：\n{}",self.data.display()))
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
    remember: Option<bool>,
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
        if remember.unwrap_or(false) {
            let preferences = window.state::<DesktopPreferences>();
            let mut next = preferences.value.lock().unwrap().clone();
            next.close_behavior = CloseBehavior::Exit;
            preferences.save(next)?;
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
    let initial_args=std::env::args().collect::<Vec<_>>();
    let app_root = std::env::var_os("FRAMEKEEP_APP_ROOT").map(PathBuf::from).unwrap_or_else(||
        std::env::current_exe().expect("无法获取程序位置").parent().unwrap().to_path_buf());
    let data = data_directory(
        &app_root,
        std::env::var_os("FRAMEKEEP_DATA").map(PathBuf::from).or_else(|| desktop_preferences::argument_data(&initial_args)),
    );
    let port = std::env::var("FRAMEKEEP_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(8769);
    let silent = desktop_preferences::silent_start(&initial_args);
    let preferences = DesktopPreferences::load(&data);
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
        splash_visible: Mutex::new(None),
    });
    let cleanup = engine.clone();
    let app=tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(move |app,args,cwd|{
            if queue_photos(&inbox_data,&args,Path::new(&cwd)){return;}
            if !desktop_preferences::startup_launch(&args) && !desktop_preferences::silent_start(&args) { show_main(app); }
        }))
        .plugin(tauri_plugin_dialog::init()).manage(engine.clone()).manage(quick_search::SearchHotkey::default()).manage(preferences)
        .invoke_handler(tauri::generate_handler![choose_folders,choose_cache_folder,choose_cache_backup,save_manifest,save_sku_workbook,finish_close,desktop_ready,close_for_update,startup_status,startup_visible,get_desktop_preferences,set_desktop_preferences,hide_to_tray,search_show,search_hide,search_shortcut,search_open_asset])
        .setup(move |app| {
            create_tray(app)?;
            queue_photos(&engine.data,&initial_args,&initial_cwd);
            let window=tauri::WebviewWindowBuilder::new(app,"main",WebviewUrl::App("index.html".into()))
                .title(if desktop_preferences::development_build() { "末帧 · 多模态资产检索开发版" } else { "末帧选片" }).inner_size(1440.,900.).min_inner_size(1000.,680.).center()
                .visible(!silent)
                // 让资源管理器文件拖放进入前端的HTML5事件，避免WebView2被原生处理器截获。
                .disable_drag_drop_handler()
                .on_navigation(move |url|url.scheme()=="tauri"||url.host_str()==Some("tauri.localhost")||(url.scheme()=="http"&&url.host_str()==Some("127.0.0.1")&&url.port_or_known_default()==Some(port)))
                .build()?;
            let events=engine.clone();let close_window=window.clone();
            window.on_window_event(move |event| {
                if let WindowEvent::CloseRequested{api,..}=event {
                    if !events.closing.load(Ordering::SeqCst) {
                        api.prevent_close();request_close(&close_window, false);
                    }
                }
            });
            let handle=app.handle().clone();
            thread::spawn(move || {
                match engine.start() {
                    Ok(())=>{
                        let shortcut=engine.request("/api/intelligence/state",false).ok()
                            .and_then(|v|v.get("config")?.get("shortcut")?.as_str().map(str::to_owned))
                            .unwrap_or_else(||"Ctrl+Shift+Space".into());
                        let search_handle=handle.clone();let search_port=engine.port;
                        let _=handle.run_on_main_thread(move||{
                            if let Err(error)=quick_search::create(&search_handle,search_port){
                                search_handle.dialog().message(error.to_string()).title("快捷搜索窗口创建失败").show(|_|{});return;
                            }
                            if let Err(error)=search_handle.state::<quick_search::SearchHotkey>().set(search_handle.clone(),&shortcut){
                                search_handle.dialog().message(error).title("快捷搜索按键注册失败").show(|_|{});
                            }
                        });
                        let delivery=engine.clone();thread::spawn(move||delivery.deliver_photos());
                        let _=window.navigate(format!("http://127.0.0.1:{}/",engine.port).parse().unwrap());
                    }
                    Err(error)=>{if !engine.closing.load(Ordering::SeqCst) {show_main(&handle);handle.dialog().message(error).title("无法打开末帧选片").blocking_show();handle.exit(1);}}
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
    fn slow_startup_waits_for_real_progress_and_remains_bounded() {
        let mut waiting=StartupWait::default();
        assert!(!waiting.expired(Duration::from_secs(35)));
        assert!(waiting.expired(Duration::from_secs(60)));
        waiting.observe(&serde_json::json!({"process_id":42,"stage":"catalog ready"}),42,Duration::from_secs(45));
        assert!(!waiting.expired(Duration::from_secs(90)));
        // 同阶段反复写入或其他进程的旧状态不能无限续期。
        waiting.observe(&serde_json::json!({"process_id":42,"stage":"catalog ready"}),42,Duration::from_secs(100));
        waiting.observe(&serde_json::json!({"process_id":99,"stage":"http ready"}),42,Duration::from_secs(100));
        assert!(waiting.expired(Duration::from_secs(105)));
        waiting.observe(&serde_json::json!({"process_id":42,"stage":"ratings ready"}),42,Duration::from_secs(170));
        assert!(waiting.expired(Duration::from_secs(180)));
    }
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
