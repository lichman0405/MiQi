"""MiQi MCP 工具 → PiEvo 实验执行接口"""
import logging
from typing import Any

logger = logging.getLogger(__name__)


def create_tool_adapter(tool_registry):
    """
    将 MiQi 的 ToolRegistry 封装为 PiEvo 需要的 async callable。

    PiEvo 接口: async (tool_name: str, params: dict) -> dict
    返回的 dict 必须包含 "outcome": float
    """

    async def pievo_tool_executor(tool_name: str, params: dict) -> dict[str, Any]:
        tool = tool_registry.get(tool_name)
        if tool is None:
            raise ValueError(f"Tool '{tool_name}' not found in registry")

        logger.info(f"PiEvo executing tool: {tool_name} with params={params}")
        result = await tool.execute(**params)

        outcome = _extract_outcome(result)
        return {
            "success": True,
            "outcome": outcome,
            "raw_result": result,
            "tool_name": tool_name,
        }

    return pievo_tool_executor


def _extract_outcome(result: Any) -> float:
    """从工具结果中提取数值型 outcome"""
    if isinstance(result, (int, float)):
        return float(result)
    if isinstance(result, dict):
        for key in ("outcome", "value", "result", "score", "energy",
                     "pore_volume", "surface_area", "g_factor", "yield",
                     "uptake", "conversion", "selectivity"):
            if key in result and isinstance(result[key], (int, float)):
                return float(result[key])
        for v in result.values():
            if isinstance(v, (int, float)):
                return float(v)
    if isinstance(result, str):
        try:
            return float(result.strip())
        except ValueError:
            pass
    logger.warning(f"Cannot extract numeric outcome from result type={type(result).__name__}")
    return 0.0
