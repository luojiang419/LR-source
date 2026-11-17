"""本软件的 ONNX OCR 明确只使用 CPU，不携带可选 CUDA/TensorRT provider。"""
from pathlib import Path
from PyInstaller.utils.hooks import collect_dynamic_libs

binaries=[(source,target) for source,target in collect_dynamic_libs('onnxruntime')
          if not any(name in Path(source).name.lower() for name in ('providers_cuda','providers_tensorrt'))]
