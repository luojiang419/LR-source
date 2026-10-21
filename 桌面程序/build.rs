fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "choose_folders",
            "save_manifest",
            "save_sku_workbook",
            "finish_close",
            "desktop_ready",
            "close_for_update",
        ]),
    ))
    .expect("无法构建桌面资源");
}
