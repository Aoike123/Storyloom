"""Consume one Chat Completions request without replaying a paid submission."""
import json
import time

from .providers import ProviderError


def read_completion(response, on_event, timeout_seconds=None):
    if 'text/event-stream' not in response.headers.get('content-type', '').lower():
        # Some compatible gateways return JSON despite stream=true. Use that one response.
        response.read()
        data = response.json()
        on_event('buffered', '')
        return data

    content = ''
    usage = {}
    finish_reason = None
    signalled_reasoning = False
    lines = []
    last_model_progress = time.monotonic()
    stall_timeout = float(timeout_seconds) if timeout_seconds is not None else None

    def reject_stalled_stream():
        if stall_timeout is not None and time.monotonic() - last_model_progress >= stall_timeout:
            raise ProviderError('模型连接后在等待时限内没有返回内容，已停止空白等待；本次请求不会自动重发。')

    def events():
        for line in response.iter_lines():
            if line == '':
                if lines:
                    yield '\n'.join(lines)
                    lines.clear()
                else:
                    reject_stalled_stream()
            elif line.startswith('data:'):
                lines.append(line[5:].lstrip(' '))
            else:
                # SSE comments are transport heartbeats, not model progress.
                reject_stalled_stream()
        if lines:
            yield '\n'.join(lines)

    for event in events():
        if event.strip() == '[DONE]':
            break
        data = json.loads(event)
        if data.get('error'):
            raise ProviderError('模型输出途中返回错误，已保留构思草稿；本次请求不会自动重发。')
        if isinstance(data.get('usage'), dict):
            usage = data['usage']
        for choice in data.get('choices') or []:
            if choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta') or {}
            if (delta.get('reasoning_content') or delta.get('reasoning')) and not signalled_reasoning:
                # Display a real activity signal, not raw internal reasoning or invented prose.
                signalled_reasoning = True
                on_event('reasoning', '')
            if delta.get('reasoning_content') or delta.get('reasoning'):
                last_model_progress = time.monotonic()
            if isinstance(delta.get('content'), str) and delta['content']:
                content += delta['content']
                last_model_progress = time.monotonic()
                if len(content) > 262144:
                    raise ProviderError('模型输出超过本次可接收范围，已停止读取；不会自动重发。')
                on_event('delta', content)
            if choice.get('finish_reason') is not None:
                finish_reason = choice['finish_reason']
                last_model_progress = time.monotonic()
        reject_stalled_stream()

    if finish_reason is None:
        raise ProviderError('模型输出连接提前结束，已保留构思草稿；请核实本次调用后再重试。')
    return {'choices': [{'finish_reason': finish_reason, 'message': {'content': content}}], 'usage': usage}
