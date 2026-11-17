"""固定底图的四方向、JPEG 和 EXIF 方向验证，参考答案只在推理后评分。"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'选片助手'))
from paddle_ocr import PaddleOcr,candidate_text
from sku_match import extract_ocr_codes,normalize
from PIL import Image


def run(fixtures,out,formats=('png',),names=()):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);root=ROOT/'.runtime/models/paddle-ocr'
    engine=PaddleOcr({'detector':root/'PP-OCRv6_medium_det/inference.onnx','recognizer':root/'PP-OCRv6_medium_rec/inference.onnx',
                      'dictionary':root/'PP-OCRv6_medium_rec/inference.yml','threads':4})
    cases=json.loads(Path(fixtures).read_text('utf8'));cases=[c for c in cases if not names or c['name'] in names]
    before={c['path']:hashlib.sha256(Path(c['path']).read_bytes()).hexdigest() for c in cases}
    report={'model':'PP-OCRv6 Medium','threads':4,'backend':'CPUExecutionProvider','base_images':len(cases),
            'limitation':'同一真实底图的受控方向变体，不作为独立实拍样本；歧义手写排除精度评分。','cases':[],'summary':[]}
    for format in formats:
        for degrees,transpose in [(0,None),(90,Image.Transpose.ROTATE_90),(180,Image.Transpose.ROTATE_180),(270,Image.Transpose.ROTATE_270)]:
            rows=[]
            for case in cases:
                with Image.open(case['path']) as source:
                    source=source.convert('RGB')
                    if transpose is not None:source=source.transpose(transpose)
                    buffer=io.BytesIO()
                    if format=='png':source.save(buffer,'PNG')
                    elif format=='jpeg':source.save(buffer,'JPEG',quality=92)
                    else:
                        exif=Image.Exif();exif[274]={0:1,90:6,180:3,270:8}[degrees]
                        source.save(buffer,'JPEG',quality=92,exif=exif)
                payload=buffer.getvalue();result=engine.recognize(payload);codes=extract_ocr_codes(candidate_text(result['text']))
                row={'name':case['name'],'degrees':degrees,'format':format,'kind':case['kind'],'scorable':case.get('scorable',True),
                     'expected':case['expected'],'payload_sha256':hashlib.sha256(payload).hexdigest(),'codes':codes,
                     'candidate_exact':normalize(case['expected']) in [normalize(c) for c in codes],'result':result}
                report['cases'].append(row);rows.append(row)
            summary={'format':format,'degrees':degrees,'correct':sum(r['candidate_exact'] for r in rows if r['scorable']),
                     'scorable':sum(r['scorable'] for r in rows),'median_ms':statistics.median(r['result']['timing']['total_ms'] for r in rows),
                     'printed_correct':sum(r['candidate_exact'] for r in rows if r['kind']=='printed'),
                     'printed_count':sum(r['kind']=='printed' for r in rows)}
            report['summary'].append(summary);print(json.dumps(summary),flush=True)
            (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    report['source_hashes']=before;report['sources_unchanged']=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in before.items())
    (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--fixtures',type=Path,default=ROOT/'build/desktop-paddle-ocr/fixtures.json')
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--formats',nargs='+',choices=['png','jpeg','exif'],default=['png']);parser.add_argument('--names',nargs='*',default=[])
    args=parser.parse_args();run(args.fixtures,args.out,args.formats,args.names)
