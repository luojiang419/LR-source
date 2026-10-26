"""AI 助手本机配置；仅读取和校验，不发送照片或模型请求。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from api_endpoints import validated_url, api_endpoint

ROOT = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    pass


def config_path():
    return Path(os.environ.get('FRAMEKEEP_CONFIG', str(ROOT / 'api key.md')))


def load_config(path=None):
    path = Path(path) if path is not None else config_path()
    try:
        text = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError):
        raise ConfigError('无法读取 AI 配置文件，请检查路径、编码和访问权限。') from None
    endpoint = re.search(r'https?://[^\s<>`"\']+', text)
    key = re.search(r'\bsk-[A-Za-z0-9_-]+', text)
    model = re.search(r'\bgpt-[A-Za-z0-9_.-]+', text)
    if not endpoint or not key or not model:
        raise ConfigError('配置文件需要 HTTP/HTTPS 接口地址、API 密钥和明确模型。')
    base = endpoint.group(0).rstrip('/，。)')
    try:base=validated_url(base)
    except ValueError as exc:raise ConfigError(str(exc)) from None
    parsed = urlsplit(base)
    url = api_endpoint(base,'responses')
    return {'url': url, 'key': key.group(0), 'model': model.group(0), 'host': parsed.hostname}


def public_config():
    """返回可展示信息；从不把密钥或配置文件正文传给前端。"""
    result = {'path': str(config_path()), 'configured': False}
    try:
        config = load_config()
        result.update(configured=True, model=config['model'], host=config['host'])
    except ConfigError as error:
        result['error'] = str(error)
    return result
