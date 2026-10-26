"""合成 2400 万像素 JPEG 的 CPU/GPU 热缓存对照；不代表 NAS 或所有 RAW 性能。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import tempfile
import time

import numpy as np
from PIL import Image
import gpu_media
import media_engine


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--runs',type=int,default=6);parser.add_argument('--output',type=Path)
    args=parser.parse_args();report={'fixture':'synthetic 6000x4000 JPEG; hot file cache; decode+resize only','results':[]}
    original=os.environ.get('FRAMEKEEP_GPU')
    try:
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'synthetic.jpg';rng=np.random.default_rng(419)
            image=Image.fromarray(rng.integers(0,256,(4000,6000,3),dtype='uint8'));image.save(source,'JPEG',quality=92)
            before=hashlib.sha256(source.read_bytes()).hexdigest()
            for edge in (480,1800):
                versions={};times={}
                for mode in ('cpu','auto'):
                    os.environ['FRAMEKEEP_GPU']=mode
                    media_engine._read_preview(source,edge)
                    elapsed=[]
                    for _ in range(max(1,args.runs)):
                        started=time.perf_counter();result=media_engine._read_preview(source,edge)
                        result=gpu_media.resize_preview(result,edge);elapsed.append((time.perf_counter()-started)*1000)
                    versions[mode]=np.asarray(result,dtype=float);times[mode]=statistics.median(elapsed)
                mse=np.mean((versions['auto']-versions['cpu'])**2)
                # 原 CPU 路径启用了 JPEG draft；另与全分辨率 CPU 解码作质量对照。
                with Image.open(source) as decoded:
                    reference=decoded.convert('RGB');reference.thumbnail((edge,edge),Image.Resampling.LANCZOS);reference=np.asarray(reference,dtype=float)
                quality={mode:float(10*np.log10(255**2/max(np.mean((pixels-reference)**2),1e-10))) for mode,pixels in versions.items()}
                report['results'].append({'edge':edge,'cpu_ms':times['cpu'],'gpu_ms':times['auto'],'speedup':times['cpu']/times['auto'],
                                          'cpu_gpu_psnr_db':float(10*np.log10(255**2/max(mse,1e-10))),
                                          'cpu_vs_full_resolution_psnr_db':quality['cpu'],'gpu_vs_full_resolution_psnr_db':quality['auto']})
            report['source_unchanged']=before==hashlib.sha256(source.read_bytes()).hexdigest();report['gpu']=gpu_media.state()
    finally:
        if original is None:os.environ.pop('FRAMEKEEP_GPU',None)
        else:os.environ['FRAMEKEEP_GPU']=original
    if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
