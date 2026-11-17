"""模型调用：OpenAI 兼容接口的薄封装，顺带记下每次调用的耗时和 token。"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from askdata import config


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    seconds: float = 0.0


@dataclass
class Reply:
    content: str
    tool_calls: list[dict] = field(default_factory=list)  # [{id, name, arguments(str)}]
    finish_reason: str | None = None


class ChatModel(Protocol):
    usage: Usage

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> Reply: ...


class OpenAIChat:
    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None):
        from openai import OpenAI

        key = api_key or config.api_key()
        if not key:
            raise RuntimeError('没有设置模型的 API Key：export OPENAI_API_KEY=...')
        self.model = model or config.model_name()
        self.base_url = base_url or config.base_url()
        self.client = OpenAI(api_key=key, base_url=self.base_url, timeout=120, max_retries=2)
        self.usage = Usage()
        self.on_call = None  # 回调 (request, reply, usage_delta, seconds)，Trace 用

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> Reply:
        kwargs: dict[str, Any] = {'model': self.model, 'messages': messages, 'temperature': 0}
        if tools:
            # 只用 auto：有的 OpenAI 兼容网关在 tool_choice=required 时会返回空的工具调用
            kwargs['tools'], kwargs['tool_choice'] = tools, 'auto'
        if 'volces.com' in self.base_url:
            kwargs['extra_body'] = {'thinking': {'type': 'disabled'}}  # 豆包默认会深度思考，问数用不上，关掉省时间
        start = time.monotonic()
        resp = self.client.chat.completions.create(**kwargs)
        seconds = time.monotonic() - start
        choice = resp.choices[0]
        msg = choice.message
        reply = Reply(
            content=msg.content or '',
            tool_calls=[{'id': c.id, 'name': c.function.name, 'arguments': c.function.arguments or '{}'} for c in msg.tool_calls or []],
            finish_reason=choice.finish_reason,
        )
        u = resp.usage
        delta = Usage(
            calls=1,
            prompt_tokens=u.prompt_tokens if u else 0,
            completion_tokens=u.completion_tokens if u else 0,
            cached_tokens=(getattr(u.prompt_tokens_details, 'cached_tokens', 0) or 0) if u and u.prompt_tokens_details else 0,
            seconds=seconds,
        )
        for k in ('calls', 'prompt_tokens', 'completion_tokens', 'cached_tokens', 'seconds'):
            setattr(self.usage, k, getattr(self.usage, k) + getattr(delta, k))
        if self.on_call:
            self.on_call(messages, reply, delta)
        return reply


def parse_json(text: str) -> dict:
    """从模型回复里取出 JSON 对象：容忍 ```json 代码块和前后的说明文字。"""
    m = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.S)
    body = m.group(1) if m else text[text.find('{') : text.rfind('}') + 1]
    if not body:
        raise ValueError('回复里没有 JSON 对象')
    return json.loads(body)
