fn main() {
    let root=std::path::PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    for name in ["startup.css","startup.svg","startup-visual.js","desktop.js","desktop-lifecycle.css"] {
        let source=root.parent().unwrap().join("演示方案").join(name);
        let target=root.join("frontend").join(name);
        println!("cargo:rerun-if-changed={}",source.display());
        let bytes=std::fs::read(source).expect("启动动画资源缺失");
        if std::fs::read(&target).ok().as_ref()!=Some(&bytes) {std::fs::write(target,bytes).expect("无法同步启动动画");}
    }
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "choose_folders",
            "save_manifest",
            "save_sku_workbook",
            "finish_close",
            "get_desktop_preferences",
            "set_desktop_preferences",
            "hide_to_tray",
            "desktop_ready",
            "startup_status",
            "startup_visible",
            "close_for_update",
            "search_show",
            "search_hide",
            "search_shortcut",
            "search_open_asset",
        ]),
    ))
    .expect("无法构建桌面资源");
}
