#!/usr/bin/env python3
"""模型上线前的真实 OpenAI 兼容推理验收请求。"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from typing import Any


# 极小 PNG 只用于触发视觉编码路径，不承担视觉准确率评测，避免四实例上线
# 验收因外部图片地址或大图片处理时间而失去确定性。
_VISION_PROBE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9WlSgAAAAASUVORK5CYII="
)


def endpoint_inference_ready(
    origin: str,
    api_key: str,
    served_model_name: str,
    timeout: int = 60,
    detail: list[str] | None = None,
    image_count: int = 0,
) -> bool:
    """发送有界生成请求，并可通过内嵌小图验证视觉编码路径。

    参数：
        origin: 不含 ``/v1`` 的 Worker 来源地址。
        api_key: 内部 Worker API Key；仅写入请求头，不进入诊断文本。
        served_model_name: 候选部署写入 vLLM 的服务模型名。
        timeout: 单个真实生成请求的最大等待秒数。
        detail: 可选诊断列表；失败时追加一条脱敏、截断的原因。
        image_count: 视觉探测图片数量；0 表示只做文本生成，最多发送 2 张。

    返回：
        HTTP 成功且响应包含有效 assistant 消息时返回真。图片数量大于 0 时，
        请求一定经过 OpenAI 多模态 ``image_url`` 输入路径。
    """

    probe_images = max(0, min(int(image_count), 2))
    content: str | list[dict[str, Any]] = "只回复 OK"
    if probe_images:
        content = [
            *(
                {
                    "type": "image_url",
                    "image_url": {"url": _VISION_PROBE_DATA_URL},
                }
                for _ in range(probe_images)
            ),
            {"type": "text", "text": "确认已收到图片，只回复 OK"},
        ]
    body = json.dumps(
        {
            "model": served_model_name,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": 16,
            "stream": False,
        },
        ensure_ascii=False,
    ).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        f"{origin.rstrip('/')}/v1/chat/completions",
        data=body,
        headers=headers,
        method="POST",
    )

    def fail(reason: str) -> bool:
        """记录不含凭据的单条失败摘要并返回假。"""

        if detail is not None:
            detail.append(" ".join(str(reason).split())[:800])
        return False

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return fail(f"HTTP {response.status}")
            payload = json.loads(response.read(2 << 20))
    except urllib.error.HTTPError as error:
        with contextlib.suppress(OSError):
            response_body = error.read(4096).decode("utf-8", errors="replace")
            return fail(f"HTTP {error.code}: {response_body}")
        return fail(f"HTTP {error.code}")
    except json.JSONDecodeError as error:
        return fail(f"响应不是有效 JSON：{error.msg}")
    except (OSError, urllib.error.URLError) as error:
        return fail(f"请求失败或超时：{error}")
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        return fail("响应缺少 choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return fail("响应缺少 assistant message")
    has_text = any(
        isinstance(message.get(field), str) and bool(message.get(field).strip())
        for field in ("content", "reasoning_content", "reasoning_text", "reasoning")
    )
    if has_text or (
        isinstance(message.get("tool_calls"), list) and bool(message["tool_calls"])
    ):
        return True
    return fail("assistant message 没有文本、思考内容或工具调用")
