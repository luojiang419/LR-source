"""在构建前准备固定哈希的 CPU OCR 归档，发布流程再上传内容寻址镜像。"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'选片助手'))
from component_setup import ComponentManager,write_json


def prepare():
    path=ROOT/'选片助手/component-manifest.json';manifest=json.loads(path.read_text('utf8'))
    manager=ComponentManager(ROOT/'build/cpu-mirror-preparation',manifest=manifest)
    def fetch(item):
        target=ROOT/'.runtime/component-archives'/item['name']
        manager._download(item['source_url'],target,item['sha256'],item.get('size'))
        item['size']=target.stat().st_size
        print(item['name']+' SHA-256 通过',flush=True)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(fetch,manifest.get('model_archives',[])))
    finally:manager.close()
    write_json(path,manifest)


if __name__=='__main__':prepare()
