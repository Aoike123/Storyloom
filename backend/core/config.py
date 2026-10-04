"""Storyloom Studio — 模型配置读取（ROOT-04 附录 09 §6 的读取入口）。

与旧 backend/environment.py 的差异：
1. 新 env 名 STUDIO_ENV_FILE、新默认文件 <仓库根>/.env.studio（旧层
   STORYLOOM_ENV_FILE/.env.local 永不读取，避免两套系统互相串配置）；
2. 纯读取：model_config() 每次调用返回即时快照（优先级 文件 > os.environ >
   DEFAULTS），绝不注入 os.environ、不创建文件；import 本模块无任何副作用。

键表沿用附录 09 §6 冻结的模型配置键（LLM_*/IMAGE_*/VIDEO_* 旧名复用）。
"""
import os
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]

DEFAULTS = {
    "LLM_BASE_URL": "https://api.deepseek.com",
    "LLM_MODEL": "deepseek-flash",
    "LLM_FAST_MODEL": "deepseek-flash",
    "LLM_API_KEY": "",
    "LLM_MAX_TOKENS": "8192",
    "LLM_FAST_MAX_TOKENS": "2048",
    "LLM_TIMEOUT": "120",
    "IMAGE_PROVIDER": "siliconflow",
    "IMAGE_ENDPOINT": "https://api.siliconflow.cn/v1/images/generations",
    "IMAGE_MODEL": "Tongyi-MAI/Z-Image-Turbo",
    "IMAGE_API_KEY": "",
    "VIDEO_PROVIDER": "minimax",
    "VIDEO_ENDPOINT": "https://api.minimax.cn/v2/video_generation",
    "VIDEO_MODEL": "MiniMax-H3-Max",
    "VIDEO_API_KEY": "",
    "VIDEO_DURATION": "8",
    "VIDEO_RESOLUTION": "768P",
}
_KEYS = set(DEFAULTS) | {"LLM_PROVIDER", "LLM_ENDPOINT"}


def env_file() -> Path:
    return Path(os.getenv("STUDIO_ENV_FILE", str(ROOT / ".env.studio")))


def load_env_file(path: Path) -> dict[str, str]:
    """纯读取；文件不存在 → {}；只取已知键。"""
    if not path.is_file():
        return {}
    return {
        k: v
        for k, v in dotenv_values(path).items()
        if k in _KEYS and v is not None
    }


def model_config() -> dict[str, str]:
    """即时快照：文件 > os.environ > DEFAULTS。不写 os.environ。"""
    configured = {k: os.environ[k] for k in _KEYS if k in os.environ}
    return {**DEFAULTS, **configured, **load_env_file(env_file())}
