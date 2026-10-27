use std::{fs, path::{Path, PathBuf}, sync::Mutex};
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CloseBehavior { #[default] Ask, Tray, Exit }

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StartupMode { #[default] Disabled, Window, Tray }

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Preferences {
    pub close_behavior: CloseBehavior,
    pub startup_mode: StartupMode,
}

pub struct DesktopPreferences {
    pub path: PathBuf,
    pub value: Mutex<Preferences>,
}

impl DesktopPreferences {
    pub fn load(data: &Path) -> Self {
        let path = data.join("desktop-preferences.json");
        let mut value: Preferences = fs::read(&path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default();
        // 卸载或系统工具移除了启动项时，不展示过期的“已开启”。
        #[cfg(windows)]
        if let Ok(mode) = registered_startup(data) { value.startup_mode = mode; }
        Self { path, value: Mutex::new(value) }
    }

    pub fn save(&self, next: Preferences) -> Result<Preferences, String> {
        let mut current = self.value.lock().unwrap();
        fs::create_dir_all(self.path.parent().unwrap()).map_err(|e| e.to_string())?;
        let temporary = self.path.with_extension("json.tmp");
        fs::write(&temporary, serde_json::to_vec_pretty(&next).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
        // 仅在用户改变启动方式时修改当前用户启动项，不因普通启动或关闭窗口重新登记。
        let registration = if next.startup_mode != current.startup_mode {
            Some(set_startup(next.startup_mode, self.path.parent().unwrap())?)
        } else { None };
        if let Err(error) = fs::rename(&temporary, &self.path) {
            if let Some(previous) = registration {
                restore_startup(previous).map_err(|e| format!("设置保存失败：{error}；启动项回滚失败：{e}"))?;
            }
            return Err(format!("设置保存失败：{error}"));
        }
        *current = next.clone();
        Ok(next)
    }
}

// 按 Windows CommandLineToArgvW 规则转义，兼容中文、空格及末尾反斜杠。
fn quote_argument(value: &str) -> String {
    let mut result = String::from("\"");
    let mut slashes = 0;
    for ch in value.chars() {
        if ch == '\\' { slashes += 1; continue; }
        result.push_str(&"\\".repeat(if ch == '"' { slashes * 2 + 1 } else { slashes }));
        result.push(ch);
        slashes = 0;
    }
    result.push_str(&"\\".repeat(slashes * 2));
    result.push('"');
    result
}

fn startup_command(exe: &Path, data: &Path, mode: StartupMode) -> Option<String> {
    if mode == StartupMode::Disabled { return None; }
    // 源码开发版依靠启动器恢复 Python、模型路径和隔离端口；系统登录不会继承启动器环境。
    if let Some(launcher) = development_launcher(exe) {
        return Some(format!("powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File {} -Startup{} -DataDirectory {}",
            quote_argument(&launcher.to_string_lossy()), if mode == StartupMode::Tray { " -Silent" } else { "" }, quote_argument(&data.to_string_lossy())));
    }
    Some(format!("{} --startup{} --data-dir {}", quote_argument(&exe.to_string_lossy()),
        if mode == StartupMode::Tray { " --silent" } else { "" }, quote_argument(&data.to_string_lossy())))
}

pub fn development_build() -> bool {
    let base: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).expect("应用配置无效");
    let overlay: serde_json::Value = option_env!("TAURI_CONFIG").and_then(|v| serde_json::from_str(v).ok()).unwrap_or_default();
    overlay.get("identifier").unwrap_or(&base["identifier"]) == "com.framekeep.culling.multimodal-dev"
}

fn development_launcher(exe: &Path) -> Option<PathBuf> {
    if !development_build() { return None; }
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).parent()?;
    let launcher = source.join("启动多模态检索开发版.ps1");
    (exe.starts_with(source.join("桌面程序/target")) && launcher.is_file()).then_some(launcher)
}

#[cfg(windows)]
const RUN_KEY: &str = "Software\\Microsoft\\Windows\\CurrentVersion\\Run";
#[cfg(windows)]
fn startup_value_name() -> &'static str { if development_build() { "FramekeepMultimodalDev" } else { "Framekeep" } }

#[cfg(windows)]
fn registered_startup(data: &Path) -> Result<StartupMode, String> {
    use winreg::{RegKey, enums::{HKEY_CURRENT_USER, KEY_READ}};
    let key = match RegKey::predef(HKEY_CURRENT_USER).open_subkey_with_flags(RUN_KEY, KEY_READ) {
        Ok(key) => key,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(StartupMode::Disabled),
        Err(e) => return Err(e.to_string()),
    };
    let command: String = match key.get_value(startup_value_name()) {
        Ok(command) => command,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(StartupMode::Disabled),
        Err(e) => return Err(e.to_string()),
    };
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    for mode in [StartupMode::Window, StartupMode::Tray] {
        if startup_command(&exe, data, mode).as_deref() == Some(command.as_str()) { return Ok(mode); }
    }
    Ok(StartupMode::Disabled)
}

#[cfg(windows)]
fn set_startup(mode: StartupMode, data: &Path) -> Result<Option<winreg::RegValue<'static>>, String> {
    use winreg::{RegKey, enums::HKEY_CURRENT_USER};
    let (key, _) = RegKey::predef(HKEY_CURRENT_USER).create_subkey(RUN_KEY).map_err(|e| format!("无法设置开机启动：{e}"))?;
    let previous = match key.get_raw_value(startup_value_name()) {
        Ok(value) => Some(value),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(e) => return Err(format!("无法读取开机启动项：{e}")),
    };
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    write_startup(&key, startup_command(&exe, data, mode).as_deref())?;
    Ok(previous)
}

#[cfg(windows)]
fn write_startup(key: &winreg::RegKey, command: Option<&str>) -> Result<(), String> {
    let result = if let Some(command) = command { key.set_value(startup_value_name(), &command) } else { key.delete_value(startup_value_name()) };
    match result { Ok(()) => Ok(()), Err(e) if command.is_none() && e.kind() == std::io::ErrorKind::NotFound => Ok(()), Err(e) => Err(format!("无法设置开机启动：{e}")) }
}

#[cfg(windows)]
fn restore_startup(previous: Option<winreg::RegValue<'static>>) -> Result<(), String> {
    let (key, _) = winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER).create_subkey(RUN_KEY).map_err(|e| e.to_string())?;
    if let Some(value) = previous { key.set_raw_value(startup_value_name(), &value).map_err(|e| e.to_string()) } else { write_startup(&key, None) }
}

#[cfg(not(windows))]
fn set_startup(_: StartupMode, _: &Path) -> Result<Option<()>, String> { Err("当前系统暂不支持开机启动设置".into()) }
#[cfg(not(windows))]
fn restore_startup(_: Option<()>) -> Result<(), String> { Ok(()) }

pub fn silent_start(args: &[String]) -> bool { args.iter().any(|v| v == "--silent") }
pub fn startup_launch(args: &[String]) -> bool { args.iter().any(|v| v == "--startup") }
pub fn argument_data(args: &[String]) -> Option<PathBuf> {
    args.windows(2).find(|pair| pair[0] == "--data-dir").map(|pair| PathBuf::from(&pair[1])).filter(|p| p.is_absolute())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn source_startup_uses_launcher_and_separate_registration() {
        let exe = Path::new(env!("CARGO_MANIFEST_DIR")).join("target/debug/framekeep-desktop.exe");
        let command = startup_command(&exe, Path::new("D:/开发版资料"), StartupMode::Tray).unwrap();
        if development_build() {
            assert!(command.starts_with("powershell.exe -NoProfile -WindowStyle Hidden"));
            assert!(command.contains("启动多模态检索开发版.ps1"));
            assert!(command.contains(" -Startup -Silent -DataDirectory \"D:/开发版资料\""));
        } else {
            assert!(command.starts_with(&quote_argument(&exe.to_string_lossy())));
            assert!(command.contains(" --startup --silent --data-dir "));
        }
        #[cfg(windows)]
        assert_eq!(startup_value_name(), if development_build() { "FramekeepMultimodalDev" } else { "Framekeep" });
    }
    #[test]
    fn defaults_and_invalid_values() {
        assert_eq!(serde_json::from_str::<Preferences>("{}").unwrap(), Preferences::default());
        assert!(serde_json::from_str::<Preferences>(r#"{"close_behavior":"invalid"}"#).is_err());
        assert!(serde_json::from_str::<Preferences>(r#"{"startup_mode":"invalid"}"#).is_err());
    }
    #[test]
    fn preferences_persist_and_corruption_falls_back_to_ask() {
        let data = std::env::temp_dir().join(format!("framekeep-prefs-{}", uuid::Uuid::new_v4()));
        let prefs = DesktopPreferences::load(&data);
        let next = Preferences { close_behavior: CloseBehavior::Tray, ..Default::default() };
        prefs.save(next.clone()).unwrap();
        assert_eq!(*DesktopPreferences::load(&data).value.lock().unwrap(), next);
        prefs.save(Preferences { close_behavior: CloseBehavior::Exit, ..Default::default() }).unwrap();
        assert_eq!(DesktopPreferences::load(&data).value.lock().unwrap().close_behavior, CloseBehavior::Exit);
        fs::write(&prefs.path, "broken").unwrap();
        assert_eq!(*DesktopPreferences::load(&data).value.lock().unwrap(), Preferences::default());
        fs::remove_file(prefs.path).unwrap(); fs::remove_dir(data).unwrap();
    }
    #[test]
    fn startup_commands_quote_paths_and_only_silent_mode_hides() {
        let exe = Path::new("C:/软件 目录/末帧.exe"); let data = Path::new("D:/照片资料");
        assert_eq!(startup_command(exe, data, StartupMode::Disabled), None);
        let normal = startup_command(exe, data, StartupMode::Window).unwrap();
        assert!(normal.starts_with("\"C:/软件 目录/末帧.exe\" --startup --data-dir \""));
        assert!(!normal.contains("--silent"));
        let silent = startup_command(exe, data, StartupMode::Tray).unwrap();
        let args = vec!["app.exe".into(), "--startup".into(), "--silent".into(), "--data-dir".into(), data.to_string_lossy().into_owned()];
        assert!(silent.contains("--startup --silent")); assert!(silent_start(&args)); assert!(startup_launch(&args));
        assert_eq!(argument_data(&args), Some(data.to_path_buf()));
        assert!(!silent_start(&["app.exe".into()]));
        assert_eq!(quote_argument("C:\\路径\\"), "\"C:\\路径\\\\\"");
    }
    #[cfg(windows)]
    #[test]
    fn windows_startup_registration_updates_and_removes_only_our_value() {
        use winreg::{RegKey, enums::HKEY_CURRENT_USER};
        let hkcu = RegKey::predef(HKEY_CURRENT_USER);
        let path = format!("Software\\FramekeepDesktopTests-{}", uuid::Uuid::new_v4());
        let (key, _) = hkcu.create_subkey(&path).unwrap();
        key.set_value("OtherApp", &"keep").unwrap();
        for mode in [StartupMode::Window, StartupMode::Tray, StartupMode::Disabled] {
            let command = startup_command(Path::new("C:/程序 目录/末帧.exe"), Path::new("D:/用户 资料"), mode);
            write_startup(&key, command.as_deref()).unwrap();
            if let Some(command) = command { assert_eq!(key.get_value::<String, _>(startup_value_name()).unwrap(), command); }
            else { assert_eq!(key.get_value::<String, _>(startup_value_name()).unwrap_err().kind(), std::io::ErrorKind::NotFound); }
            assert_eq!(key.get_value::<String, _>("OtherApp").unwrap(), "keep");
        }
        write_startup(&key, None).unwrap();
        drop(key); hkcu.delete_subkey(&path).unwrap();
    }
}
