"""生成可离线打开的 HTML 验证报告；数据均来自实际执行快照。"""
import base64
from datetime import datetime
import html
import io
import json
from pathlib import Path

from PIL import Image, ImageOps

from core import ROOT


def render(run, evidence):
    run = Path(run)
    thumbs = []
    for sample in evidence['samples']:
        if sample['name'].lower().endswith('.jpg'):
            with Image.open(sample['baseline']) as source:
                image = ImageOps.exif_transpose(source).convert('RGB')
                image.thumbnail((620, 500))
                stream = io.BytesIO()
                image.save(stream, format='JPEG', quality=83)
            thumbs.append(dict(name=sample['name'], src='data:image/jpeg;base64,' + base64.b64encode(stream.getvalue()).decode()))
    model = dict(evidence, thumbs=thumbs)
    template = Path(__file__).with_name('report.html').read_text(encoding='utf-8')
    payload = json.dumps(model, ensure_ascii=False).replace('<', '\\u003c')
    design = (ROOT / '技术文档' / '异地多机拍摄同步技术方案.md').read_text(encoding='utf-8')
    target = run / '多机拍摄同步验证报告.html'
    target.write_text(template.replace('/*__EVIDENCE__*/', payload).replace('<!--__DESIGN__-->', html.escape(design)), encoding='utf-8')
    return target


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    print(render(args.run, json.loads((args.run / 'evidence.json').read_text(encoding='utf-8'))))
