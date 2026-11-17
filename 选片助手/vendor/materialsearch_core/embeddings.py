"""供宿主资产库调用的独立特征接口；不导入扫描、数据库或环境配置。

基于 MaterialSearch / Yumin Li 的图文特征流程改造。
与 MaterialSearch Core 一同按 LGPL-3.0-only 分发，见仓库 LICENSE。
"""
from threading import RLock


def _load_components(model_name, device, local_files_only):
    # 仅导入本接口时不初始化 Torch、探测 GPU 或下载模型。
    from transformers import AutoModelForZeroShotImageClassification, AutoProcessor

    processor = AutoProcessor.from_pretrained(model_name, local_files_only=local_files_only)
    model = AutoModelForZeroShotImageClassification.from_pretrained(
        model_name, local_files_only=local_files_only
    ).to(device).eval()
    return model, processor


class FeatureEncoder:
    """延迟加载并串行使用模型，返回单位化的 float32 二维特征矩阵。

    输入为宿主已解码的 RGB 图片列表或文字列表，不读取业务库和归档文件。
    RAW、PSD、视频抽帧及源版本校验由宿主负责；建议放在独立工作进程中。
    """

    def __init__(self, model_name, *, device="cpu", local_files_only=True,
                 dimension=None, max_batch_size=8, max_text_length=52):
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("模型名称或本地目录不能为空")
        for name, value in (("max_batch_size", max_batch_size),
                            ("max_text_length", max_text_length)):
            if type(value) is not int or value < 1:
                raise ValueError(name + " 必须为正整数")
        if dimension is not None and (type(dimension) is not int or dimension < 1):
            raise ValueError("dimension 必须为正整数")
        if type(local_files_only) is not bool:
            raise ValueError("local_files_only 必须为布尔值")
        self.model_name = model_name
        self.device = device
        self.local_files_only = local_files_only
        self.max_batch_size = max_batch_size
        self.max_text_length = max_text_length
        self.dimension = dimension
        self._lock = RLock()
        self._model = self._processor = None

    @property
    def loaded(self):
        with self._lock:
            return self._model is not None

    def _components(self):
        if self._model is None:
            model, processor = _load_components(self.model_name, self.device, self.local_files_only)
            self._model, self._processor = model, processor
        return self._model, self._processor

    def _batch(self, values):
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError("请提供非空的图片或文字列表")
        if len(values) > self.max_batch_size:
            raise ValueError("批次超过 max_batch_size，请由宿主拆分")
        if any(value is None for value in values):
            raise ValueError("批次包含空输入")
        return list(values)

    def _normalize(self, features, rows):
        import torch

        if not isinstance(features, torch.Tensor):
            features = getattr(features, "pooler_output", None)
        if not isinstance(features, torch.Tensor) or features.ndim != 2:
            raise ValueError("模型未返回二维投影特征")
        features = features.float()
        if features.shape[0] != rows or features.shape[1] < 1:
            raise ValueError("模型返回的特征行数或维数不正确")
        if self.dimension is not None and features.shape[1] != self.dimension:
            raise ValueError("图像和文字特征维数与当前索引不一致")
        norms = features.norm(dim=1, keepdim=True)
        if not torch.isfinite(features).all() or not torch.isfinite(norms).all() or (norms <= 0).any():
            raise ValueError("模型返回零向量或非有限特征")
        normalized = (features / norms).detach().cpu().numpy()
        self.dimension = features.shape[1]
        return normalized

    def encode_images(self, images):
        """图片应先按宿主统一的方向、RGB 和预览规格处理。"""
        images = self._batch(images)
        with self._lock:
            model, processor = self._components()
            import torch

            with torch.inference_mode():
                inputs = processor(images=images, return_tensors="pt")
                inputs = {key: value.to(self.device) for key, value in inputs.items()}
                return self._normalize(model.get_image_features(**inputs), len(images))

    def encode_texts(self, texts):
        texts = self._batch(texts)
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("文字批次包含空字符串或非文字输入")
        with self._lock:
            model, processor = self._components()
            import torch

            with torch.inference_mode():
                inputs = processor(text=texts, return_tensors="pt", padding=True,
                                   truncation=True, max_length=self.max_text_length)
                # 保留 attention_mask 等处理器返回的模型输入。
                inputs = {key: value.to(self.device) for key, value in inputs.items()}
                return self._normalize(model.get_text_features(**inputs), len(texts))

    def close(self):
        """释放模型引用；不停止宿主线程，不更改全局 GPU/线程配置。"""
        with self._lock:
            self._model = self._processor = None
