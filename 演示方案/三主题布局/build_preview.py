"""用现有样片构建独立演示及聊天预览，不连接业务服务。"""

import argparse
import base64
import io
import json
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"


def image_data(path: Path, width: int) -> str:
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((width, width * 2))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=78, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inline", type=Path, help="本次聊天可视化目录中的输出文件")
    args = parser.parse_args()
    originals = json.loads((ASSETS / "demo-data.js").read_text(encoding="utf-8").split("=", 1)[1].strip().rstrip(";"))
    numbers = [929, 932, 937, 940, 943, 944, 947, 948, 951, 954, 957, 960]
    photos = []
    for i, number in enumerate(numbers):
        name = f"XSY_{number:04}"
        original = next(row for row in originals if row["id"] == name)
        photos.append({
            "id": i,
            "name": name,
            "time": original["time"].split("T")[1],
            "rating": {1: 4, 3: 2, 5: 3, 8: 5}.get(i, 0),
            "src": image_data(ASSETS / f"{name}.jpg", 440),
            "thumb": image_data(ASSETS / f"{name}.jpg", 100),
        })
    template = (HERE / "preview.template.html").read_text(encoding="utf-8")
    fragment = template.replace("__FRAMEKEEP_PHOTOS__", json.dumps(photos, ensure_ascii=False, separators=(",", ":")))
    assert "__FRAMEKEEP_PHOTOS__" not in fragment
    assert len(fragment.encode("utf-8")) < 1_000_000
    page = '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>末帧 · SHIYIN 三主题布局演示</title><link rel="icon" href="data:,"><style>html,body{margin:0;background:#f6eee4}body{min-width:320px}</style><script src="lucide.js"></script></head><body>\n' + fragment + '\n</body></html>\n'
    (HERE / "index.html").write_text(page, encoding="utf-8")
    if args.inline:
        args.inline.parent.mkdir(parents=True, exist_ok=True)
        args.inline.write_text(fragment, encoding="utf-8")
    print(f"已生成独立演示；{len(photos)} 张已有样片；预览 {len(fragment.encode('utf-8')):,} 字节。")


if __name__ == "__main__":
    main()
