"""可选的 nvJPEG / CUDA 图像管线；没有兼容设备时保持既有 CPU 路径。"""
from contextlib import contextmanager
import ctypes as C
import os
import queue
from pathlib import Path
import sys
import threading

from PIL import Image

_LOCK=threading.RLock()
_SLOTS=threading.BoundedSemaphore(2)
_BACKEND=None
_ATTEMPTED=False
_STATUS={'ready':False,'backend':'cpu','device':'','jpeg_gpu':0,'jpeg_hybrid_gpu':0,'jpeg_encode_gpu':0,'resize_gpu':0,'fallbacks':0,'last_error':''}

KERNEL=r'''
__device__ float lanczos(float x) {
    x=fabsf(x); if(x<0.00001f)return 1.f; if(x>=3.f)return 0.f;
    float p=3.14159265358979323846f*x;
    return (sinf(p)/p)*(sinf(p/3.f)/(p/3.f));
}
extern "C" __global__ void horizontal(const unsigned char* src,float* tmp,int w,int h,int dw) {
    int x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if(x>=dw||y>=h)return;
    float ratio=(float)w/dw, factor=fmaxf(1.f,ratio), center=(x+.5f)*ratio-.5f;
    int left=max(0,(int)ceilf(center-3.f*factor)),right=min(w-1,(int)floorf(center+3.f*factor));
    float r=0,g=0,b=0,total=0;
    for(int i=left;i<=right;i++) {
        float a=lanczos((i-center)/factor);int p=(y*w+i)*3;
        r+=src[p]*a;g+=src[p+1]*a;b+=src[p+2]*a;total+=a;
    }
    int p=(y*dw+x)*3;
    tmp[p]=r/total;tmp[p+1]=g/total;tmp[p+2]=b/total;
}
extern "C" __global__ void vertical(const float* tmp,unsigned char* dst,int dw,int sh,int dh,int orientation) {
    bool swap=orientation>=5;int ow=swap?dh:dw,oh=swap?dw:dh;
    int x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if(x>=ow||y>=oh)return;
    int rx=x,ry=y;
    if(orientation==2)rx=dw-1-x;
    else if(orientation==3){rx=dw-1-x;ry=dh-1-y;}
    else if(orientation==4)ry=dh-1-y;
    else if(orientation==5){rx=y;ry=x;}
    else if(orientation==6){rx=y;ry=dh-1-x;}
    else if(orientation==7){rx=dw-1-y;ry=dh-1-x;}
    else if(orientation==8){rx=dw-1-y;ry=x;}
    float ratio=(float)sh/dh,factor=fmaxf(1.f,ratio),center=(ry+.5f)*ratio-.5f;
    int top=max(0,(int)ceilf(center-3.f*factor)),bottom=min(sh-1,(int)floorf(center+3.f*factor));
    float r=0,g=0,b=0,total=0;
    for(int i=top;i<=bottom;i++) {
        float a=lanczos((i-center)/factor);int p=(i*dw+rx)*3;
        r+=tmp[p]*a;g+=tmp[p+1]*a;b+=tmp[p+2]*a;total+=a;
    }
    int p=(y*ow+x)*3;
    dst[p]=(unsigned char)fminf(255.f,fmaxf(0.f,roundf(r/total)));
    dst[p+1]=(unsigned char)fminf(255.f,fmaxf(0.f,roundf(g/total)));
    dst[p+2]=(unsigned char)fminf(255.f,fmaxf(0.f,roundf(b/total)));
}
'''


class GPUError(RuntimeError):pass


class JpegImage(C.Structure):
    _fields_=[('channel',C.c_void_p*4),('pitch',C.c_size_t*4)]


def runtime_folder():
    root=Path(__file__).resolve().parents[1]
    resource=Path(getattr(sys,'_MEIPASS',root))
    for path in (resource/'runtime/gpu',root/'.runtime/gpu',Path(sys.executable).parent/'runtime/gpu'):
        if (path/'nvjpeg64_12.dll').is_file():return path
    raise GPUError('未部署 CUDA 图像组件，使用 CPU')


def _bind(library,name,args,returns=C.c_int):
    fn=getattr(library,name);fn.argtypes=args;fn.restype=returns;return fn


def _check(code,operation):
    if code:raise GPUError(f'{operation} 失败 ({code})')


def _dimensions(width,height,edge):
    ratio=min(1.,edge/max(width,height))
    return max(1,round(width*ratio)),max(1,round(height*ratio))


class CudaImages:
    def __init__(self):
        if os.name!='nt':raise GPUError('当前图像 GPU 后端适用于 Windows NVIDIA')
        folder=runtime_folder()
        from setup_gpu import verify_runtime
        if not verify_runtime(folder):raise GPUError('CUDA 图像组件校验失败，使用 CPU')
        self.directory=os.add_dll_directory(str(folder))
        self.driver=C.WinDLL('nvcuda.dll',winmode=0x800);self.runtime=C.WinDLL(str(folder/'cudart64_12.dll'))
        self.jpeg=C.CDLL(str(folder/'nvjpeg64_12.dll'))
        self.builtins=C.WinDLL(str(folder/'nvrtc-builtins64_129.dll'))
        self.rtc=C.CDLL(str(folder/'nvrtc64_120_0.dll'))
        ptr=C.c_void_p;u64=C.c_uint64;integer=C.c_int;size=C.c_size_t
        self.init=_bind(self.driver,'cuInit',[C.c_uint])
        self.device_get=_bind(self.driver,'cuDeviceGet',[C.POINTER(integer),integer])
        self.retain=_bind(self.driver,'cuDevicePrimaryCtxRetain',[C.POINTER(ptr),integer])
        self.current=_bind(self.driver,'cuCtxSetCurrent',[ptr])
        self.alloc=_bind(self.driver,'cuMemAlloc_v2',[C.POINTER(u64),size])
        self.free=_bind(self.driver,'cuMemFree_v2',[u64])
        self.upload=_bind(self.driver,'cuMemcpyHtoD_v2',[u64,ptr,size])
        self.download=_bind(self.driver,'cuMemcpyDtoH_v2',[ptr,u64,size])
        self.stream_create=_bind(self.driver,'cuStreamCreate',[C.POINTER(ptr),C.c_uint])
        self.stream_destroy=_bind(self.driver,'cuStreamDestroy_v2',[ptr])
        self.synchronize=_bind(self.driver,'cuStreamSynchronize',[ptr])
        self.module_load=_bind(self.driver,'cuModuleLoadData',[C.POINTER(ptr),ptr])
        self.function_get=_bind(self.driver,'cuModuleGetFunction',[C.POINTER(ptr),ptr,C.c_char_p])
        self.launch=_bind(self.driver,'cuLaunchKernel',[ptr,*([C.c_uint]*7),ptr,C.POINTER(ptr),ptr])
        self.jpeg_create_ex=_bind(self.jpeg,'nvjpegCreateEx',[integer,ptr,ptr,C.c_uint,C.POINTER(ptr)])
        # 严格码流校验与插值色度上采样，避免简单色度复制损伤细节。
        self.jpeg_create=lambda target:self.jpeg_create_ex(0,None,None,36,target)
        self.jpeg_destroy=_bind(self.jpeg,'nvjpegDestroy',[ptr])
        self.state_create=_bind(self.jpeg,'nvjpegJpegStateCreate',[ptr,C.POINTER(ptr)])
        self.state_destroy=_bind(self.jpeg,'nvjpegJpegStateDestroy',[ptr])
        self.info=_bind(self.jpeg,'nvjpegGetImageInfo',[ptr,ptr,size,C.POINTER(integer),C.POINTER(integer),C.POINTER(integer),C.POINTER(integer)])
        self.decode=_bind(self.jpeg,'nvjpegDecode',[ptr,ptr,ptr,size,integer,ptr,ptr])
        self.decoder_create=_bind(self.jpeg,'nvjpegDecoderCreate',[ptr,integer,C.POINTER(ptr)])
        self.decoder_state_create=_bind(self.jpeg,'nvjpegDecoderStateCreate',[ptr,ptr,C.POINTER(ptr)])
        self.parsed_create=_bind(self.jpeg,'nvjpegJpegStreamCreate',[ptr,C.POINTER(ptr)])
        self.parse=_bind(self.jpeg,'nvjpegJpegStreamParse',[ptr,ptr,size,integer,integer,ptr])
        self.params_create=_bind(self.jpeg,'nvjpegDecodeParamsCreate',[ptr,C.POINTER(ptr)])
        self.params_format=_bind(self.jpeg,'nvjpegDecodeParamsSetOutputFormat',[ptr,integer])
        self.supported=_bind(self.jpeg,'nvjpegDecoderJpegSupported',[ptr,ptr,ptr,C.POINTER(integer)])
        self.decode_host=_bind(self.jpeg,'nvjpegDecodeJpegHost',[ptr,ptr,ptr,ptr,ptr])
        self.decode_transfer=_bind(self.jpeg,'nvjpegDecodeJpegTransferToDevice',[ptr,ptr,ptr,ptr,ptr])
        self.decode_device=_bind(self.jpeg,'nvjpegDecodeJpegDevice',[ptr,ptr,ptr,ptr,ptr])
        self.device_buffer_create=_bind(self.jpeg,'nvjpegBufferDeviceCreate',[ptr,ptr,C.POINTER(ptr)])
        self.pinned_buffer_create=_bind(self.jpeg,'nvjpegBufferPinnedCreate',[ptr,ptr,C.POINTER(ptr)])
        self.attach_device=_bind(self.jpeg,'nvjpegStateAttachDeviceBuffer',[ptr,ptr])
        self.attach_pinned=_bind(self.jpeg,'nvjpegStateAttachPinnedBuffer',[ptr,ptr])
        self.encode_state=_bind(self.jpeg,'nvjpegEncoderStateCreateWithBackend',[ptr,C.POINTER(ptr),integer,ptr])
        self.encode_state_destroy=_bind(self.jpeg,'nvjpegEncoderStateDestroy',[ptr])
        self.encode_params=_bind(self.jpeg,'nvjpegEncoderParamsCreate',[ptr,C.POINTER(ptr),ptr])
        self.encode_params_destroy=_bind(self.jpeg,'nvjpegEncoderParamsDestroy',[ptr])
        self.encode_quality=_bind(self.jpeg,'nvjpegEncoderParamsSetQuality',[ptr,integer,ptr])
        self.encode_sampling=_bind(self.jpeg,'nvjpegEncoderParamsSetSamplingFactors',[ptr,integer,ptr])
        self.encode=_bind(self.jpeg,'nvjpegEncodeImage',[ptr,ptr,ptr,ptr,integer,integer,integer,ptr])
        self.bitstream=_bind(self.jpeg,'nvjpegEncodeRetrieveBitstream',[ptr,ptr,ptr,C.POINTER(size),ptr])
        _check(self.init(0),'CUDA 初始化');device=integer();_check(self.device_get(C.byref(device),0),'选择 GPU')
        self.context=ptr();_check(self.retain(C.byref(self.context),device),'GPU 上下文');self.activate()
        name=C.create_string_buffer(256);_check(_bind(self.driver,'cuDeviceGetName',[ptr,integer,integer])(name,256,device),'GPU 名称')
        self.name=name.value.decode(errors='replace')
        self.module=ptr();ptx=self._compile();_check(self.module_load(C.byref(self.module),ptx),'加载缩放内核')
        self.horizontal=ptr();self.vertical=ptr()
        _check(self.function_get(C.byref(self.horizontal),self.module,b'horizontal'),'横向缩放内核')
        _check(self.function_get(C.byref(self.vertical),self.module,b'vertical'),'纵向缩放内核')
        self.sessions=queue.LifoQueue(2)
        for _ in range(2):self.sessions.put(self._new_session())

    def activate(self):_check(self.current(self.context),'激活 GPU 上下文')

    def _new_session(self):
        session={key:C.c_void_p() for key in ('handle','legacy','decoder','state','parsed','params','device','pinned','stream')}
        _check(self.jpeg_create(C.byref(session['handle'])),'nvJPEG 初始化')
        handle=session['handle']
        _check(self.state_create(handle,C.byref(session['legacy'])),'JPEG 兼容状态')
        _check(self.decoder_create(handle,2,C.byref(session['decoder'])),'GPU 混合解码器')
        _check(self.decoder_state_create(handle,session['decoder'],C.byref(session['state'])),'GPU 解码状态')
        _check(self.parsed_create(handle,C.byref(session['parsed'])),'JPEG 解析器')
        _check(self.params_create(handle,C.byref(session['params'])),'JPEG 解码参数')
        _check(self.params_format(session['params'],5),'RGB 输出')
        _check(self.device_buffer_create(handle,None,C.byref(session['device'])),'JPEG 设备缓冲')
        _check(self.pinned_buffer_create(handle,None,C.byref(session['pinned'])),'JPEG 固定内存')
        _check(self.attach_device(session['state'],session['device']),'连接设备缓冲')
        _check(self.attach_pinned(session['state'],session['pinned']),'连接固定内存')
        _check(self.stream_create(C.byref(session['stream']),1),'GPU 流')
        return session

    def _compile(self):
        ptr=C.c_void_p;program=ptr()
        create=_bind(self.rtc,'nvrtcCreateProgram',[C.POINTER(ptr),C.c_char_p,C.c_char_p,C.c_int,ptr,ptr])
        compile_program=_bind(self.rtc,'nvrtcCompileProgram',[ptr,C.c_int,C.POINTER(C.c_char_p)])
        destroy=_bind(self.rtc,'nvrtcDestroyProgram',[C.POINTER(ptr)])
        _check(create(C.byref(program),KERNEL.encode(),b'framekeep-resize.cu',0,None,None),'创建缩放编译器')
        try:
            options=(C.c_char_p*2)(b'--gpu-architecture=compute_75',b'--std=c++11')
            result=compile_program(program,len(options),options)
            if result:
                length=C.c_size_t();_bind(self.rtc,'nvrtcGetProgramLogSize',[ptr,C.POINTER(C.c_size_t)])(program,C.byref(length))
                log=C.create_string_buffer(length.value);_bind(self.rtc,'nvrtcGetProgramLog',[ptr,ptr])(program,log)
                raise GPUError('GPU 缩放内核编译失败：'+log.value.decode(errors='replace')[-1000:])
            length=C.c_size_t();_check(_bind(self.rtc,'nvrtcGetPTXSize',[ptr,C.POINTER(C.c_size_t)])(program,C.byref(length)),'内核大小')
            ptx=C.create_string_buffer(length.value);_check(_bind(self.rtc,'nvrtcGetPTX',[ptr,ptr])(program,ptx),'读取缩放内核')
            return ptx
        finally:destroy(C.byref(program))

    @contextmanager
    def memory(self):
        allocated=[]
        def allocate(length):
            value=C.c_uint64();_check(self.alloc(C.byref(value),length),'分配显存');allocated.append(value);return value
        try:yield allocate
        finally:
            for value in reversed(allocated):self.free(value)

    def _kernel(self,function,width,height,stream,values):
        args=(C.c_void_p*len(values))(*(C.cast(C.byref(value),C.c_void_p) for value in values))
        _check(self.launch(function,(width+15)//16,(height+15)//16,1,16,16,1,0,stream,args,None),'GPU 缩放')

    def _resize(self,source,width,height,edge,orientation,stream,allocate):
        dw,dh=_dimensions(width,height,edge);swap=orientation>=5;ow,oh=(dh,dw) if swap else (dw,dh)
        intermediate=allocate(dw*height*3*4);output=allocate(dw*dh*3)
        integer=C.c_int
        self._kernel(self.horizontal,dw,height,stream,[source,intermediate,integer(width),integer(height),integer(dw)])
        self._kernel(self.vertical,ow,oh,stream,[intermediate,output,integer(dw),integer(height),integer(dh),integer(orientation)])
        _check(self.synchronize(stream),'等待 GPU 缩放')
        pixels=C.create_string_buffer(ow*oh*3);_check(self.download(pixels,output,len(pixels)),'读取 GPU 预览')
        return Image.frombytes('RGB',(ow,oh),pixels.raw)

    def image(self,image,edge):
        self.activate();width,height=image.size
        if width*height>100_000_000:raise GPUError('超大图片使用内存受控 CPU 路径')
        pixels=image.convert('RGB').tobytes();stream=C.c_void_p();_check(self.stream_create(C.byref(stream),1),'GPU 流')
        try:
            with self.memory() as allocate:
                source=allocate(len(pixels));host=C.create_string_buffer(pixels,len(pixels));_check(self.upload(source,host,len(pixels)),'上传图片')
                _check(self.synchronize(None),'等待图片上传')
                return self._resize(source,width,height,edge,1,stream,allocate)
        finally:self.stream_destroy(stream)

    def jpeg_image(self,payload,edge,orientation):
        self.activate();session=self.sessions.get();handle=session['handle'];stream=session['stream']
        try:
            encoded=C.create_string_buffer(payload,len(payload));components=C.c_int();subsampling=C.c_int();widths=(C.c_int*4)();heights=(C.c_int*4)()
            _check(self.info(handle,encoded,len(payload),C.byref(components),C.byref(subsampling),widths,heights),'JPEG 信息')
            width,height=widths[0],heights[0]
            if components.value not in (1,3) or not 0<width*height<=100_000_000:raise GPUError('JPEG 格式使用 CPU 兼容路径')
            with self.memory() as allocate:
                source=allocate(width*height*3);output=JpegImage();output.channel[0]=source.value;output.pitch[0]=width*3
                _check(self.parse(handle,encoded,len(payload),0,1,session['parsed']),'JPEG 码流解析')
                unsupported=C.c_int(1)
                _check(self.supported(session['decoder'],session['parsed'],session['params'],C.byref(unsupported)),'JPEG GPU 能力检查')
                if unsupported.value==0:
                    _check(self.decode_host(handle,session['decoder'],session['state'],session['params'],session['parsed']),'JPEG 主机阶段')
                    _check(self.decode_transfer(handle,session['decoder'],session['state'],session['parsed'],stream),'JPEG GPU 传输')
                    _check(self.decode_device(handle,session['decoder'],session['state'],C.byref(output),stream),'JPEG GPU 解码')
                    _record('jpeg_hybrid_gpu')
                else:
                    _check(self.decode(handle,session['legacy'],encoded,len(payload),5,C.byref(output),stream),'nvJPEG 兼容解码')
                result=self._resize(source,width,height,edge,orientation,stream,allocate)
                return result
        finally:
            self.synchronize(stream);self.sessions.put(session)

    def encode_jpeg(self,image,quality):
        self.activate();handle=C.c_void_p();state=C.c_void_p();params=C.c_void_p();stream=C.c_void_p()
        _check(self.jpeg_create(C.byref(handle)),'nvJPEG 初始化')
        try:
            _check(self.stream_create(C.byref(stream),1),'GPU 流')
            _check(self.encode_state(handle,C.byref(state),1,stream),'GPU JPEG 编码器')
            _check(self.encode_params(handle,C.byref(params),stream),'JPEG 编码参数')
            _check(self.encode_quality(params,int(quality),stream),'JPEG 质量')
            _check(self.encode_sampling(params,2,stream),'JPEG 色度采样')
            image=image.convert('RGB');width,height=image.size
            if width*height>100_000_000:raise GPUError('超大图片使用 CPU 编码')
            pixels=image.tobytes()
            with self.memory() as allocate:
                source=allocate(len(pixels));host=C.create_string_buffer(pixels,len(pixels));_check(self.upload(source,host,len(pixels)),'上传编码图片')
                _check(self.synchronize(None),'等待编码图片上传')
                rgb=JpegImage();rgb.channel[0]=source.value;rgb.pitch[0]=width*3
                _check(self.encode(handle,state,params,C.byref(rgb),5,width,height,stream),'GPU JPEG 编码')
                length=C.c_size_t();_check(self.bitstream(handle,state,None,C.byref(length),stream),'JPEG 大小')
                payload=C.create_string_buffer(length.value);_check(self.bitstream(handle,state,payload,C.byref(length),stream),'读取 GPU JPEG')
                _check(self.synchronize(stream),'等待 JPEG 编码');return payload.raw[:length.value]
        finally:
            if stream:self.synchronize(stream)
            if params:self.encode_params_destroy(params)
            if state:self.encode_state_destroy(state)
            if stream:self.stream_destroy(stream)
            self.jpeg_destroy(handle)


def backend():
    global _BACKEND,_ATTEMPTED
    if os.environ.get('FRAMEKEEP_GPU','auto').lower() in ('off','0','cpu'):return None
    with _LOCK:
        if not _ATTEMPTED:
            _ATTEMPTED=True
            try:
                _BACKEND=CudaImages();_STATUS.update(ready=True,backend='cuda-nvjpeg',device=_BACKEND.name)
            except (OSError,ValueError,RuntimeError,AttributeError) as error:_STATUS['last_error']=str(error)
        return _BACKEND


def _record(key,error=None):
    with _LOCK:
        _STATUS[key]+=1
        if error:_STATUS['last_error']=str(error)[-500:]


def decode_jpeg(payload,edge,orientation=1):
    gpu=backend()
    if gpu is None:return None
    try:
        with _SLOTS:result=gpu.jpeg_image(payload,int(edge),int(orientation or 1))
        _record('jpeg_gpu');return result
    except (OSError,ValueError,RuntimeError) as error:_record('fallbacks',error);return None


def resize_preview(image,edge):
    if max(image.size)<=edge:return image
    gpu=backend()
    if gpu is not None:
        try:
            with _SLOTS:result=gpu.image(image,int(edge))
            _record('resize_gpu');return result
        except (OSError,ValueError,RuntimeError) as error:_record('fallbacks',error)
    image.thumbnail((edge,edge),Image.Resampling.LANCZOS);return image


def encode_jpeg(image,quality=95):
    gpu=backend()
    if gpu is None:return None
    try:
        with _SLOTS:result=gpu.encode_jpeg(image,quality)
        _record('jpeg_encode_gpu');return result
    except (OSError,ValueError,RuntimeError) as error:_record('fallbacks',error);return None


def state():
    with _LOCK:result=dict(_STATUS)
    result['enabled']=os.environ.get('FRAMEKEEP_GPU','auto').lower() not in ('off','0','cpu')
    result['available']=result['ready']
    if not result['enabled']:result.update(ready=False,backend='cpu')
    result['raw_unpack']='cpu';result['webp_encoding']='disabled';result['preview_codec']='embedded-jpeg';result['max_preview_edge']=2048;result['gpu_slots']=2
    return result


def warmup():
    def run():
        gpu=backend()
        if gpu is not None:
            try:
                with _SLOTS:gpu.image(Image.new('RGB',(32,32)),16)
            except (OSError,ValueError,RuntimeError) as error:_record('fallbacks',error)
    threading.Thread(target=run,name='gpu-media-warmup',daemon=True).start()
