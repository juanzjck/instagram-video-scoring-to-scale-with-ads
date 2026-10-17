"""One shared Anthropic client. No sampling parameters are sent: current models reject them."""

import time
from functools import lru_cache

import anthropic

from .config import MODEL, WORKSPACE_ID


@lru_cache(maxsize=1)
def client():
    return anthropic.Anthropic(max_retries=4)


def complete(content, system=None, max_tokens=1500, json_schema=None, stream=False):
    """Single user turn. `content` is a string or a list of content blocks (text + images).

    stream=True is for long outputs: the reply arrives as it is written, so a request that
    takes minutes is not dropped as an idle connection. The return value is the same.

    Returns {"text", "input_tokens", "output_tokens", "latency_s", "model", "stop_reason"}.
    """
    kwargs = dict(model=MODEL, max_tokens=max_tokens,
                  messages=[{"role": "user", "content": content}])
    if system:
        kwargs["system"] = system
    if json_schema:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}
    if WORKSPACE_ID:
        kwargs["workspace_id"] = WORKSPACE_ID

    t0 = time.time()
    if stream:
        with client().messages.stream(**kwargs) as s:
            resp = s.get_final_message()
    else:
        resp = client().messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        raise RuntimeError("the model declined this request (stop_reason=refusal)")
    return {
        "text": "".join(b.text for b in resp.content if b.type == "text"),
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "latency_s": round(time.time() - t0, 2),
        "model": MODEL,
        "stop_reason": resp.stop_reason,
    }
