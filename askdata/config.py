"""全局配置：数据位置、“今天”、模型接口。都可以用环境变量覆盖。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get('ASKDATA_DB', ROOT / 'data' / 'shop.db'))
DICT_PATH = Path(os.environ.get('ASKDATA_DICT', ROOT / 'data' / 'dictionary.json'))

# 演示数据截止到 2026-06-30，“今天”固定在这一天，“上周”“本月”才有确定的答案
TODAY = os.environ.get('ASKDATA_TODAY', '2026-06-30')

DEFAULT_BASE_URL = 'https://ark.cn-beijing.volces.com/api/v3'
DEFAULT_MODEL = 'doubao-seed-2-1-pro-260915'


def model_name() -> str:
    return os.environ.get('ASKDATA_MODEL', DEFAULT_MODEL)


def base_url() -> str:
    return os.environ.get('ASKDATA_BASE_URL') or os.environ.get('OPENAI_BASE_URL') or DEFAULT_BASE_URL


def api_key() -> str | None:
    return os.environ.get('ASKDATA_API_KEY') or os.environ.get('OPENAI_API_KEY')
