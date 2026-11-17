"""显示预览固定按需读取文件；只在当前响应中持有像素，不发布磁盘缓存。"""
from contextlib import nullcontext
from io import BytesIO
from PIL import Image
import media_engine as media
from preview_codec import MAX_PREVIEW_EDGE,save_preview


def preview_bytes(record,root,exiftool,size=480,decode=False):
    size=max(160,min(MAX_PREVIEW_EDGE,int(size)))
    if size<=480 and not decode and not record.get('_video') and not record.get('_design'):
        from live_thumbnails import thumbnail_bytes
        return thumbnail_bytes(record,root,exiftool,size)
    with media._PREVIEW_SLOTS,media._DECODE_SLOT if decode else nullcontext():
        if record.get('_video'):
            from video_preview import extract_frame
            image=extract_frame(record['_video'],size,record['_video_frame'])
        elif record.get('_design'):
            from design_preview import preview
            image=preview(record['_design'],size)
        elif decode and record.get('raw'):
            import rawpy
            with rawpy.imread(str(media.resolved_file(root,record['raw']))) as source:
                image=Image.fromarray(source.postprocess(use_camera_wb=True,half_size=True))
        else:
            image=media._companion_preview(record,root,size)
            if image is None and record.get('raw'):
                tool=exiftool() if callable(exiftool) else exiftool
                path=media.resolved_file(root,record['raw'])
                orientation,payload=media.raw_embedded_metadata(path,tool)
                image=media._read_preview(BytesIO(payload),size,orientation) if payload else media._raw_preview(dict(record,orientation=orientation),root,tool,size)
            if image is None and record.get('_fallback_raw'):image=media._paired_raw_preview(dict(record),exiftool,size)
            if image is None:raise RuntimeError('源文件没有可用预览，请检查文件是否在线。')
        image.thumbnail((size,size),Image.Resampling.LANCZOS)
        output=BytesIO();save_preview(image,output);return output.getvalue()


def state():
    return {'mode':'file','direct_only':True,'decode_enabled':False,'images':0,'bytes':0,
            'pending':0,'failed':0,'limit_gb':0,'path':'','job':{'status':'idle','message':'文件直读，不构建预览缓存'}}
