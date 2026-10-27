"""在独立模型 Python 中运行公开 transformers Chinese-CLIP API。"""
import base64
import io
import json
import os
from pathlib import Path
import sys


def main():
    print(json.dumps({'stage':'importing'}),flush=True)
    # torch/transformers 的诊断走 stderr，stdout 仅输出行协议。
    import contextlib
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from PIL import Image
        from transformers import ChineseCLIPModel, ChineseCLIPProcessor
        root = Path(sys.argv[1])
        hub = root/'huggingface/hub/models--OFA-Sys--chinese-clip-vit-large-patch14-336px/snapshots'
        model_dir = next(path.parent for path in hub.glob('*/config.json'))
        device = os.environ.get('FRAMEKEEP_CLIP_DEVICE','cpu')
        if device not in ('cpu','cuda'):raise ValueError('CLIP 设备应为 cpu 或 cuda')
        torch.set_num_threads(4)
        print(json.dumps({'stage':'processor'}),file=sys.__stdout__,flush=True)
        processor = ChineseCLIPProcessor.from_pretrained(str(model_dir), local_files_only=True)
        print(json.dumps({'stage':'model'}),file=sys.__stdout__,flush=True)
        model = ChineseCLIPModel.from_pretrained(str(model_dir), local_files_only=True).to(device).eval()
    print(json.dumps({'stage':'ready'}),flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with torch.inference_mode(), contextlib.redirect_stdout(sys.stderr):
                if request.get('image'):
                    image = Image.open(io.BytesIO(base64.b64decode(request['image']))).convert('RGB')
                    inputs = processor(images=image, return_tensors='pt').to(device)
                    vector = model.get_image_features(**inputs)
                else:
                    inputs = processor(text=[request['text']], padding=True, truncation=True,
                                       max_length=52, return_tensors='pt').to(device)
                    vector = model.get_text_features(**inputs)
                if not isinstance(vector,torch.Tensor):vector=vector.pooler_output
                vector = vector/vector.norm(dim=-1, keepdim=True)
            print(json.dumps({'vector': vector[0].cpu().tolist()}), flush=True)
        except Exception as exc:
            print(json.dumps({'error': str(exc)[:400]}), flush=True)


if __name__ == '__main__':
    try:main()
    except Exception as exc:print(json.dumps({'error': 'Chinese-CLIP 加载失败：'+str(exc)[:350]}), flush=True)
