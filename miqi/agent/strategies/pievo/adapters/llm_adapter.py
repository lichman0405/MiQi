"""MiQi LLM Provider → PiEvo LLM 调用接口"""
from typing import Any


def create_llm_adapter(
    provider_chat,  # MiQi provider.chat() 方法
    *,
    model: str = "",
    temperature: float = 0.6,
    max_tokens: int = 4096,
):
    """
    将 MiQi 的 LLM provider 封装为 PiEvo 需要的 async callable。

    PiEvo 接口: async (system_prompt: str, user_message: str) -> str
    """

    async def pievo_llm_call(system_prompt: str, user_message: str) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        # MiQi provider.chat() 接受 messages + 可选参数
        # 返回字符串（模型响应文本）
        result = await provider_chat(
            messages=messages,
            model=model or None,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        # 处理可能的不同返回类型
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            return result.get("content", str(result))
        return str(result)

    return pievo_llm_call
