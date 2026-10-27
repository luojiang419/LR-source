//! 独立快捷搜索窗口与 Windows 全局快捷键。只管理本进程注册的按键。
use std::sync::Mutex;
use tauri::{AppHandle, Manager, WebviewUrl};

pub struct SearchHotkey(Mutex<Option<Registration>>);

impl Default for SearchHotkey {
    fn default() -> Self { Self(Mutex::new(None)) }
}

impl SearchHotkey {
    pub fn set(&self, app: AppHandle, shortcut: &str) -> Result<(), String> {
        let mut current = self.0.lock().map_err(|e| e.to_string())?;
        if current.as_ref().map(|r| r.shortcut.as_str()) == Some(shortcut) { return Ok(()); }
        if shortcut == "off" { current.take(); return Ok(()); }
        // 新快捷键先注册成功，再释放旧键；冲突时保留原快捷键。
        let registered = Registration::new(app, shortcut)?;
        *current = Some(registered);
        Ok(())
    }
}

pub fn show(app: &AppHandle) -> Result<(), String> {
    let window = app.get_webview_window("asset-search").ok_or("快捷搜索窗口尚未就绪")?;
    if let Some(main) = app.get_webview_window("main") {
        if let Ok(Some(monitor)) = main.current_monitor() {
            let _ = window.set_position(*monitor.position());
            let _ = window.set_size(*monitor.size());
        }
    }
    window.show().map_err(|e| e.to_string())?;
    window.set_focus().map_err(|e| e.to_string())?;
    let _ = window.eval("window.FramekeepRetrieval?.wake?.()");
    Ok(())
}

pub fn create(app: &AppHandle, port: u16) -> Result<(), Box<dyn std::error::Error>> {
    let url = format!("http://127.0.0.1:{port}/quick-search.html").parse()?;
    let window = tauri::WebviewWindowBuilder::new(app, "asset-search", WebviewUrl::External(url))
        .title("末帧 · 快捷资产检索").visible(false).decorations(false).transparent(true)
        .always_on_top(true).skip_taskbar(true).inner_size(1440., 900.).resizable(false)
        .disable_drag_drop_handler()
        .on_navigation(move |url| url.scheme()=="http" && url.host_str()==Some("127.0.0.1") && url.port_or_known_default()==Some(port))
        .build()?;
    #[cfg(windows)]
    {
        // 原生 Acrylic 模糊窗口后方桌面；HTML 只绘制中央搜索面板。
        let _ = window.set_effects(tauri::window::EffectsBuilder::new()
            .effect(tauri::window::Effect::Acrylic).color((15, 20, 30, 100).into()).build());
    }
    let closing = window.clone();
    window.on_window_event(move |event| {
        if let tauri::WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();let _ = closing.hide();
        }
    });
    Ok(())
}

#[cfg(windows)]
struct Registration { thread: u32, shortcut: String }

#[cfg(windows)]
mod win {
    use std::ffi::c_void;
    #[repr(C)]
    pub struct Point { pub x: i32, pub y: i32 }
    #[repr(C)]
    pub struct Message { pub hwnd: *mut c_void, pub message: u32, pub wparam: usize,
                         pub lparam: isize, pub time: u32, pub point: Point, pub private: u32 }
    #[link(name="user32")]
    extern "system" {
        pub fn RegisterHotKey(hwnd: *mut c_void, id: i32, modifiers: u32, key: u32) -> i32;
        pub fn UnregisterHotKey(hwnd: *mut c_void, id: i32) -> i32;
        pub fn GetMessageW(message: *mut Message, hwnd: *mut c_void, min: u32, max: u32) -> i32;
        pub fn PeekMessageW(message: *mut Message, hwnd: *mut c_void, min: u32, max: u32, remove: u32) -> i32;
        pub fn PostThreadMessageW(thread: u32, message: u32, wparam: usize, lparam: isize) -> i32;
    }
    #[link(name="kernel32")]
    extern "system" { pub fn GetCurrentThreadId() -> u32; }
}

#[cfg(windows)]
impl Registration {
    fn new(app: AppHandle, shortcut: &str) -> Result<Self, String> {
        let (modifiers, key) = match shortcut {
            "Ctrl+Shift+Space" => (0x0002 | 0x0004, 0x20),
            "Alt+Space" => (0x0001, 0x20),
            "Ctrl+Alt+F" => (0x0002 | 0x0001, 0x46),
            _ => return Err("不支持的搜索快捷键".into()),
        };
        let (send, receive) = std::sync::mpsc::sync_channel(1);
        std::thread::spawn(move || unsafe {
            let mut message: win::Message = std::mem::zeroed();
            win::PeekMessageW(&mut message, std::ptr::null_mut(), 0, 0, 0);
            if win::RegisterHotKey(std::ptr::null_mut(), 0x419, modifiers | 0x4000, key) == 0 {
                let _ = send.send(Err("快捷键已被其他程序占用，请选择另一组按键".to_string()));return;
            }
            if send.send(Ok(win::GetCurrentThreadId())).is_err() {
                win::UnregisterHotKey(std::ptr::null_mut(), 0x419);return;
            }
            while win::GetMessageW(&mut message, std::ptr::null_mut(), 0, 0) > 0 {
                if message.message == 0x0312 {
                    let handle = app.clone();
                    let _ = app.run_on_main_thread(move || {
                        if let Some(window) = handle.get_webview_window("asset-search") {
                            if window.is_visible().unwrap_or(false) { let _ = window.hide(); }
                            else { let _ = show(&handle); }
                        }
                    });
                }
            }
            win::UnregisterHotKey(std::ptr::null_mut(), 0x419);
        });
        let thread = receive.recv_timeout(std::time::Duration::from_secs(5))
            .map_err(|_| "注册快捷键超时".to_string())??;
        Ok(Self { thread, shortcut: shortcut.into() })
    }
}

#[cfg(windows)]
impl Drop for Registration {
    fn drop(&mut self) { unsafe { win::PostThreadMessageW(self.thread, 0x0012, 0, 0); } }
}

#[cfg(not(windows))]
struct Registration { shortcut: String }
#[cfg(not(windows))]
impl Registration {
    fn new(_: AppHandle, _: &str) -> Result<Self, String> { Err("当前全局快捷键仅支持 Windows".into()) }
}

#[tauri::command]
pub fn search_show(app: AppHandle) -> Result<(), String> { show(&app) }

#[tauri::command]
pub fn search_hide(window: tauri::WebviewWindow) -> Result<(), String> {
    if window.label() != "asset-search" { return Err("无效搜索窗口".into()); }
    window.hide().map_err(|e| e.to_string())
}

#[tauri::command]
pub fn search_shortcut(app: AppHandle, hotkey: tauri::State<'_, SearchHotkey>, shortcut: String) -> Result<(), String> {
    hotkey.set(app, &shortcut)
}

#[tauri::command]
pub fn search_open_asset(app: AppHandle, item_id: String) -> Result<(), String> {
    if item_id.len() != 32 || !item_id.chars().all(|c| c.is_ascii_hexdigit()) { return Err("资产标识无效".into()); }
    let main = app.get_webview_window("main").ok_or("主窗口尚未就绪")?;
    main.unminimize().map_err(|e| e.to_string())?;main.show().map_err(|e| e.to_string())?;
    main.set_focus().map_err(|e| e.to_string())?;
    main.eval(format!("window.FramekeepRetrieval?.previewAsset('{}')", item_id)).map_err(|e| e.to_string())?;
    if let Some(search) = app.get_webview_window("asset-search") { let _ = search.hide(); }
    Ok(())
}
