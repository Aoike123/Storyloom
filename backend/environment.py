"""One environment file for runtime settings; no credentials are exposed by this module."""
import os
from pathlib import Path
from dotenv import dotenv_values,load_dotenv

ROOT=Path(__file__).resolve().parents[1]
DEFAULTS={
    'LLM_BASE_URL':'https://api.openai-next.com/v1','LLM_MODEL':'','LLM_FAST_MODEL':'',
    'LLM_READER_MODEL':'','LLM_READER_REVIEW_MODEL':'',
    'LLM_API_KEY':'','LLM_MAX_TOKENS':'8192','LLM_FAST_MAX_TOKENS':'2048','LLM_TIMEOUT':'120',
    'LLM_READER_MAX_TOKENS':'4096','LLM_READER_REVIEW_MAX_TOKENS':'1536','LLM_READER_TIMEOUT':'90',
    'IMAGE_PROVIDER':'siliconflow','IMAGE_ENDPOINT':'https://api.siliconflow.cn/v1/images/generations',
    'IMAGE_MODEL':'Tongyi-MAI/Z-Image-Turbo','IMAGE_API_KEY':'',
    'VIDEO_PROVIDER':'minimax','VIDEO_ENDPOINT':'https://api.minimax.cn/v2/video_generation',
    'VIDEO_MODEL':'MiniMax-H3-Max','VIDEO_API_KEY':'','VIDEO_DURATION':'8','VIDEO_RESOLUTION':'768P',
    'ALLOW_PAID_CALLS':'false',
}
def env_file():
    return Path(os.getenv('STORYLOOM_ENV_FILE',str(ROOT/'.env.local')))

def load_bootstrap_environment():
    load_dotenv(env_file(),override=False)

def model_config():
    # Return a per-request snapshot. Changing one model must not mutate another request's environment.
    keys=set(DEFAULTS)|{'LLM_PROVIDER','LLM_ENDPOINT'}
    configured={k:os.environ[k] for k in keys if k in os.environ}
    saved={k:v for k,v in dotenv_values(env_file()).items() if k in keys and v is not None}
    configured.update(saved)
    # Existing installations store the full chat endpoint. Preserve that provider
    # when upgrading to the base-URL setting instead of sending its key elsewhere.
    legacy=saved.get('LLM_ENDPOINT') if 'LLM_BASE_URL' not in saved else None
    if legacy is None and 'LLM_BASE_URL' not in configured:
        legacy=configured.get('LLM_ENDPOINT')
    if legacy:
        configured['LLM_BASE_URL']=legacy.rstrip('/').removesuffix('/chat/completions')
    values={**DEFAULTS,**configured}
    return values
