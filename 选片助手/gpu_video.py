"""经真实编码探测后选择视频 GPU 后端，按素材保留软件兼容路径。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

_LOCK=threading.Lock()
_CAPS={}
_COUNTS={'gpu_previews':0,'preview_fallbacks':0}


def enabled():return os.environ.get('FRAMEKEEP_GPU','auto').lower() not in ('off','0','cpu')


def run(command,timeout=12):
    return subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,
                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def compatible_tool(name,default):
    root=Path(__file__).resolve().parents[1];resource=Path(getattr(sys,'_MEIPASS',root))
    for folder in (resource/'runtime/ffmpeg-compat',root/'.runtime/ffmpeg-compat'):
        if (folder/'source.json').is_file():
            from setup_gpu_video import verify_runtime
            # 检查结果按目录缓存，避免每次播放都重读约 200 MB 程序。
            with _LOCK:
                key=('verified',str(folder))
                if key not in _CAPS:_CAPS[key]=verify_runtime(folder)
                if _CAPS[key]:return str(folder/(name+'.exe'))
    return default


def capabilities(ffmpeg):
    if not enabled():return {'ready':False,'backend':'cpu','tool':ffmpeg,'error':'GPU 已关闭'}
    tool=compatible_tool('ffmpeg',ffmpeg)
    with _LOCK:
        if tool in _CAPS:return dict(_CAPS[tool])
        result={'ready':False,'backend':'cpu','tool':tool,'error':''}
        try:
            test=run([tool,'-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=black:s=256x144:r=1:d=1',
                      '-vf','format=nv12,hwupload_cuda,scale_cuda=256:144','-frames:v','1','-c:v','h264_nvenc','-preset','p4','-f','null','-'])
            if test.returncode==0:result.update(ready=True,backend='cuda-nvenc')
            else:result['error']=test.stderr.decode(errors='replace').strip()[-500:]
        except (OSError,subprocess.TimeoutExpired) as error:result['error']=str(error)
        _CAPS[tool]=result;return dict(result)


def source_info(source,ffprobe):
    try:
        result=run([ffprobe,'-v','error','-probesize','1048576','-analyzeduration','1000000','-select_streams','v:0',
                    '-show_entries','stream=codec_name,pix_fmt,width,height','-of','json',str(source)],timeout=8)
        return json.loads(result.stdout).get('streams',[{}])[0]
    except (OSError,ValueError,IndexError,subprocess.TimeoutExpired):return {}


def decode_supported(info):
    # 消费级 NVDEC 不支持 ProRes/DNxHR 或 4:2:2；不能仅凭扩展名判断。
    return info.get('codec_name') in ('h264','hevc','av1','vp9','mpeg2video','vc1') and info.get('pix_fmt') in ('yuv420p','yuvj420p','yuv420p10le','nv12','p010le')


def gpu_scale(edge,height=None):
    return f"scale_cuda=w='min({edge},iw)':h='min({height or edge},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2:format=nv12:interp_algo=lanczos"


def playback_candidates(source,start,ffmpeg,ffprobe):
    cpu_input=['-threads','2','-readrate','1.5','-ss',f'{start:.3f}','-i',str(source)]
    cpu_filter="scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
    software=(ffmpeg,cpu_input,['-vf',cpu_filter,'-filter_threads','1'],['-c:v','libx264','-threads','2','-preset','veryfast','-tune','zerolatency','-crf','23','-pix_fmt','yuv420p'],'cpu')
    caps=capabilities(ffmpeg)
    if not caps['ready']:return [software]
    probe=compatible_tool('ffprobe',ffprobe);info=source_info(source,probe)
    # NVENC 对极小画面有最小尺寸要求，保留原分辨率而非强制放大。
    if min(info.get('width',0),info.get('height',0))<144:return [software]
    encoder=['-c:v','h264_nvenc','-preset','p4','-tune','ll','-rc','vbr','-cq','23','-b:v','0','-bf','0']
    candidates=[]
    if decode_supported(info):
        candidates.append((caps['tool'],['-hwaccel','cuda','-hwaccel_output_format','cuda',*cpu_input],
                           ['-vf',gpu_scale(1920,1080)],encoder,'cuda-decode-scale-nvenc'))
    # 专业素材在 CPU 解码后上传 GPU，硬件缩放/编码；驱动或格式失败再降级。
    candidates.append((caps['tool'],cpu_input,['-vf','format=nv12,hwupload_cuda,'+gpu_scale(1920,1080),'-filter_threads','1'],encoder,'cuda-scale-nvenc'))
    candidates.append(software);return candidates


def extract_frame(source,edge,frame,ffmpeg,ffprobe):
    caps=capabilities(ffmpeg)
    if not caps['ready']:return None
    info=source_info(source,compatible_tool('ffprobe',ffprobe))
    if not info:return None
    for hardware in ([True,False] if decode_supported(info) else [False]):
        filters=f"select=eq(n\\,{frame-1}),"+('' if hardware else 'format=nv12,hwupload_cuda,')+f"{gpu_scale(edge)},hwdownload,format=nv12,format=rgb24"
        try:
            result=run([caps['tool'],'-nostdin','-hide_banner','-loglevel','error',*(['-hwaccel','cuda','-hwaccel_output_format','cuda'] if hardware else []),
                        '-probesize','1048576','-analyzeduration','1000000','-i',str(source),'-map','0:v:0','-an','-sn','-dn',
                        '-vf',filters,'-filter_threads','1','-frames:v','1','-fps_mode','vfr','-f','image2pipe','-c:v','png','pipe:1'],timeout=30)
            if result.returncode==0 and result.stdout:
                with _LOCK:_COUNTS['gpu_previews']+=1
                return result.stdout
        except (OSError,subprocess.TimeoutExpired):pass
        with _LOCK:_COUNTS['preview_fallbacks']+=1
    return None


def state():
    with _LOCK:
        values=[value for key,value in _CAPS.items() if isinstance(key,str)]
        result=dict(values[-1]) if values else {'ready':False,'backend':'pending' if enabled() else 'cpu'}
        result.pop('tool',None);result.update(_COUNTS);return result


def warmup(ffmpeg):
    threading.Thread(target=lambda:capabilities(ffmpeg),name='gpu-video-warmup',daemon=True).start()
