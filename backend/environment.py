"""One environment file for runtime settings; no credentials are exposed by this module.

The workbench is local: every model key and endpoint comes from the operator's own settings
file (``STORYLOOM_ENV_FILE``, or ``.env.local`` in the project root). There is no hosted demo
mode and no per-visitor credentials, so ``model_config`` simply returns that local configuration.
"""
import os
from pathlib import Path
from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    'LLM_BASE_URL': 'https://api.deepseek.com', 'LLM_MODEL': 'deepseek-flash', 'LLM_FAST_MODEL': 'deepseek-flash',
    'LLM_API_KEY': '', 'LLM_MAX_TOKENS': '8192', 'LLM_FAST_MAX_TOKENS': '2048', 'LLM_TIMEOUT': '120',
    'IMAGE_PROVIDER': 'siliconflow', 'IMAGE_ENDPOINT': 'https://api.siliconflow.cn/v1/images/generations',
    'IMAGE_MODEL': 'Tongyi-MAI/Z-Image-Turbo', 'IMAGE_API_KEY': '',
    'VIDEO_PROVIDER': 'minimax', 'VIDEO_ENDPOINT': 'https://api.minimax.cn/v2/video_generation',
    'VIDEO_MODEL': 'MiniMax-H3-Max', 'VIDEO_API_KEY': '', 'VIDEO_DURATION': '8', 'VIDEO_RESOLUTION': '768P',
}


def env_file():
    return Path(os.getenv('STORYLOOM_ENV_FILE', str(ROOT / '.env.local')))


def load_bootstrap_environment():
    load_dotenv(env_file(), override=False)


def base_model_config():
    # Return a per-request snapshot. Changing one model must not mutate another request's environment.
    keys = set(DEFAULTS) | {'LLM_PROVIDER', 'LLM_ENDPOINT'}
    configured = {k: os.environ[k] for k in keys if k in os.environ}
    saved = {k: v for k, v in dotenv_values(env_file()).items() if k in keys and v is not None}
    configured.update(saved)
    return {**DEFAULTS, **configured}


def model_config():
    return base_model_config()
