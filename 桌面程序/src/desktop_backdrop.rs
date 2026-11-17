//! 每次唤起前读取当前显示器画面，仅返回内存 PNG；不存盘、不上传。
use base64::Engine;
use std::ptr::null_mut;
use windows_sys::Win32::Graphics::Gdi::*;

struct Capture { screen:HDC, memory:HDC, bitmap:HBITMAP, previous:HGDIOBJ }
impl Drop for Capture {
    fn drop(&mut self) { unsafe {
        if !self.previous.is_null() { SelectObject(self.memory,self.previous); }
        if !self.bitmap.is_null() { DeleteObject(self.bitmap); }
        if !self.memory.is_null() { DeleteDC(self.memory); }
        if !self.screen.is_null() { ReleaseDC(null_mut(),self.screen); }
    }}
}

pub fn capture(x:i32,y:i32,width:u32,height:u32)->Result<String,String> {
    if width==0 || height==0 || width>32768 || height>32768 {return Err("显示器尺寸无效".into());}
    // 模糊背景无需原始 4K/8K 分辨率，控制主线程耗时与内存。
    let scale=(1440.0/width as f64).min(900.0/height as f64).min(1.0);
    let w=(width as f64*scale).round().max(1.0) as i32;
    let h=(height as f64*scale).round().max(1.0) as i32;
    let mut pixels=vec![0u8;(w*h*4) as usize];
    unsafe {
        let mut resource=Capture{screen:GetDC(null_mut()),memory:null_mut(),bitmap:null_mut(),previous:null_mut()};
        if resource.screen.is_null(){return Err("无法读取桌面画面".into());}
        resource.memory=CreateCompatibleDC(resource.screen);
        resource.bitmap=CreateCompatibleBitmap(resource.screen,w,h);
        if resource.memory.is_null()||resource.bitmap.is_null(){return Err("无法创建桌面画布".into());}
        resource.previous=SelectObject(resource.memory,resource.bitmap);
        if resource.previous.is_null()||resource.previous as isize == -1 {resource.previous=null_mut();return Err("无法选择桌面画布".into());}
        SetStretchBltMode(resource.memory,HALFTONE);
        if StretchBlt(resource.memory,0,0,w,h,resource.screen,x,y,width as i32,height as i32,SRCCOPY|CAPTUREBLT)==0 {return Err("桌面画面采样失败".into());}
        // GetDIBits 要求 bitmap 不再选入 DC。
        SelectObject(resource.memory,resource.previous);resource.previous=null_mut();
        let mut info:BITMAPINFO=std::mem::zeroed();
        info.bmiHeader.biSize=std::mem::size_of::<BITMAPINFOHEADER>() as u32;
        info.bmiHeader.biWidth=w;info.bmiHeader.biHeight=-h;
        info.bmiHeader.biPlanes=1;info.bmiHeader.biBitCount=32;info.bmiHeader.biCompression=BI_RGB;
        if GetDIBits(resource.memory,resource.bitmap,0,h as u32,pixels.as_mut_ptr().cast(),&mut info,DIB_RGB_COLORS)!=h {return Err("桌面画面转换失败".into());}
    }
    for pixel in pixels.chunks_exact_mut(4){pixel.swap(0,2);pixel[3]=255;}
    let mut bytes=Vec::new();
    {
        let mut encoder=png::Encoder::new(&mut bytes,w as u32,h as u32);
        encoder.set_color(png::ColorType::Rgba);encoder.set_depth(png::BitDepth::Eight);
        encoder.set_compression(png::Compression::Fast);
        let mut writer=encoder.write_header().map_err(|e|e.to_string())?;
        writer.write_image_data(&pixels).map_err(|e|e.to_string())?;
    }
    Ok(format!("data:image/png;base64,{}",base64::engine::general_purpose::STANDARD.encode(bytes)))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn capture_is_valid_bounded_memory_png() {
        let start=std::time::Instant::now();
        let source=capture(0,0,640,400).expect("桌面采样");
        let bytes=base64::engine::general_purpose::STANDARD.decode(source.strip_prefix("data:image/png;base64,").unwrap()).unwrap();
        let reader=png::Decoder::new(std::io::Cursor::new(bytes)).read_info().unwrap();
        assert_eq!((reader.info().width,reader.info().height),(640,400));
        assert!(capture(0,0,0,100).is_err());
        println!("640x400 memory desktop capture: {:?}",start.elapsed());
    }
}
