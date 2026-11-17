use std::{fs, io::{Read,Write}, net::{SocketAddr,TcpStream}, path::Path, sync::Arc, time::Duration};
use tauri::{Emitter,WebviewWindow};
use tauri_plugin_dialog::DialogExt;
use crate::Engine;

#[tauri::command]
pub async fn save_archive_document(window:WebviewWindow,engine:tauri::State<'_,Arc<Engine>>,suggested_name:String,query:String)->Result<bool,String>{
    if query.len()>8192 || query.bytes().any(|c|!c.is_ascii_alphanumeric()&&!b"%&=+-_.*".contains(&c)) {return Err("归档导出参数无效".into())}
    let port=engine.port;
    tauri::async_runtime::spawn_blocking(move || {
        let name=Path::new(&suggested_name).file_name().and_then(|s|s.to_str()).unwrap_or("拍摄记录.xlsx");
        let Some(path)=window.dialog().file().set_title("保存拍摄文档").set_parent(&window).set_file_name(name).add_filter("Excel 拍摄文档",&["xlsx"]).blocking_save_file() else {return Ok(false)};
        let mut target=path.into_path().map_err(|e|e.to_string())?;
        if target.extension().is_none(){target.set_extension("xlsx");}
        let _=window.emit("archive-document-progress","正在整理拍摄记录与图片，请稍候…");
        let mut stream=TcpStream::connect_timeout(&SocketAddr::from(([127,0,0,1],port)),Duration::from_secs(3)).map_err(|e|e.to_string())?;
        stream.set_read_timeout(Some(Duration::from_secs(600))).map_err(|e|e.to_string())?;
        stream.set_write_timeout(Some(Duration::from_secs(3))).map_err(|e|e.to_string())?;
        stream.write_all(format!("GET /api/archive-capture/document?{query} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n").as_bytes()).map_err(|e|e.to_string())?;
        let mut response=Vec::new();stream.read_to_end(&mut response).map_err(|e|format!("读取拍摄文档失败：{e}"))?;
        let start=response.windows(4).position(|w|w==b"\r\n\r\n").ok_or("拍摄文档响应无效")?+4;
        if !response.starts_with(b"HTTP/1.0 200 ")&&!response.starts_with(b"HTTP/1.1 200 "){
            let reason=serde_json::from_slice::<serde_json::Value>(&response[start..]).ok().and_then(|v|v["error"].as_str().map(str::to_owned)).unwrap_or_else(||"拍摄文档生成失败，请重试".into());return Err(reason)
        }
        if !response[start..].starts_with(b"PK"){return Err("拍摄文档格式无效，未覆盖目标文件".into())}
        let _=window.emit("archive-document-progress","正在保存拍摄文档…");
        let temporary=target.with_file_name(format!(".{}.{}.tmp",target.file_name().and_then(|s|s.to_str()).unwrap_or("拍摄记录.xlsx"),uuid::Uuid::new_v4()));
        let result=(||->Result<(),std::io::Error>{
            let mut file=fs::OpenOptions::new().write(true).create_new(true).open(&temporary)?;
            file.write_all(&response[start..])?;file.sync_all()?;drop(file);
            fs::rename(&temporary,&target)
        })();
        if let Err(error)=result{let _=fs::remove_file(&temporary);return Err(format!("保存失败，原目标文件保持：{error}"))}
        Ok(true)
    }).await.map_err(|e|e.to_string())?
}
