"""One environment file for runtime settings; no credentials are exposed by this module."""
import os
from pathlib import Path
from dotenv import dotenv_values,load_dotenv

ROOT=Path(__file__).resolve().parents[1]
DEFAULTS={
    'LLM_BASE_URL':'https://api.deepseek.com','LLM_MODEL':'deepseek-v4-flash','LLM_FAST_MODEL':'deepseek-v4-flash',
    'LLM_API_KEY':'','LLM_MAX_TOKENS':'8192','LLM_FAST_MAX_TOKENS':'2048','LLM_TIMEOUT':'120',
    'IMAGE_PROVIDER':'siliconflow','IMAGE_ENDPOINT':'https://api.siliconflow.cn/v1/images/generations',
    'IMAGE_MODEL':'Tongyi-MAI/Z-Image-Turbo','IMAGE_API_KEY':'',
    'VIDEO_PROVIDER':'minimax','VIDEO_ENDPOINT':'https://api.minimax.cn/v2/video_generation',
    'VIDEO_MODEL':'MiniMax-H3-Max','VIDEO_API_KEY':'','VIDEO_DURATION':'8','VIDEO_RESOLUTION':'768P',
    'ALLOW_PAID_CALLS':'false',
}

# Public visitors may supply credentials, but never transport targets or model
# identifiers. Keeping this contract in code prevents a browser request from
# redirecting operator or visitor keys to an arbitrary endpoint.
LOCKED_MODEL_CONFIG={
    'LLM_PROVIDER':'deepseek','LLM_BASE_URL':'https://api.deepseek.com',
    'LLM_MODEL':'deepseek-v4-flash','LLM_FAST_MODEL':'deepseek-v4-flash',
    'IMAGE_PROVIDER':'siliconflow','IMAGE_ENDPOINT':'https://api.siliconflow.cn/v1/images/generations',
    'IMAGE_MODEL':'Tongyi-MAI/Z-Image-Turbo',
    'VIDEO_PROVIDER':'minimax','VIDEO_ENDPOINT':'https://api.minimax.cn/v2/video_generation',
    'VIDEO_MODEL':'MiniMax-H3-Max','VIDEO_DURATION':'8','VIDEO_RESOLUTION':'768P',
}
def env_file():
    return Path(os.getenv('STORYLOOM_ENV_FILE',str(ROOT/'.env.local')))

def load_bootstrap_environment():
    if os.getenv('STORYLOOM_DEMO_MODE','local').lower()!='public':
        load_dotenv(env_file(),override=False)

def base_model_config():
    # Return a per-request snapshot. Changing one model must not mutate another request's environment.
    keys=set(DEFAULTS)|{'LLM_PROVIDER','LLM_ENDPOINT'}
    configured={k:os.environ[k] for k in keys if k in os.environ}
    # A hosted demo is controlled exclusively by deployment secrets. Ignore an
    # old local settings file that may remain in the persistent data volume.
    saved={} if os.getenv('STORYLOOM_DEMO_MODE','local').lower()=='public' else {
        k:v for k,v in dotenv_values(env_file()).items() if k in keys and v is not None}
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


def model_config():
    from .model_access import effective_model_config

    return effective_model_config(base_model_config())
