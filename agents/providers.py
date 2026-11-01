"""可切换的模型提供方：把 base_url + model + api_key 打包成一个可选项。

能用的提供方按 OpenAI 兼容接口对接；缺少地址或额度的只列出、不可选，理由一并带出。
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PROVIDER = "deepseek"


@dataclass(frozen=True)
class Provider:
    """一家模型服务商的一整套配置。"""

    name: str
    label: str
    base_url: str
    model: str
    api_key: str
    #: 界面上的补充说明（比如"免费档""仅占位"）
    note: str = ""

    @property
    def available(self) -> bool:
        """缺任何一样都不能选 —— 与其让用户点了再报错，不如现在就说明。"""
        return bool(self.api_key.strip() and self.base_url.strip() and self.model.strip())

    @property
    def reason(self) -> str:
        """不可用的原因（可用时为空串）。"""
        missing: list[str] = []
        if not self.api_key.strip():
            missing.append("API key")
        if not self.base_url.strip():
            missing.append("接口地址")
        if not self.model.strip():
            missing.append("模型名")
        if not missing:
            return ""
        return "、".join(missing) + " 还没配置"

    def public(self) -> dict[str, object]:
        """给界面的视图：**绝不包含 key**。"""
        return {
            "name": self.name,
            "label": self.label,
            "model": self.model,
            "note": self.note,
            "available": self.available,
            "reason": self.reason,
        }


def providers_from_settings(settings: object) -> tuple[Provider, ...]:
    """按固定顺序给出全部提供方（第一个是默认）。"""
    return (
        Provider(
            name="deepseek",
            label="DeepSeek",
            base_url=str(getattr(settings, "deepseek_base_url", "") or ""),
            model=str(getattr(settings, "deepseek_model", "") or ""),
            api_key=str(getattr(settings, "deepseek_api_key", "") or ""),
            note="默认",
        ),
        Provider(
            name="glm",
            label="GLM（智谱）",
            base_url=str(getattr(settings, "glm_base_url", "") or ""),
            model=str(getattr(settings, "glm_model", "") or ""),
            api_key=str(getattr(settings, "glm_api_key", "") or ""),
            note="OpenAI 兼容，工具与流式都支持",
        ),
        Provider(
            name="mimo",
            label="MiMo",
            base_url=str(getattr(settings, "mimo_base_url", "") or ""),
            model=str(getattr(settings, "mimo_model", "") or ""),
            api_key=str(getattr(settings, "mimo_api_key", "") or ""),
            note="占位：还没配置接口地址与模型名",
        ),
    )


def find_provider(providers: tuple[Provider, ...], name: str) -> Provider | None:
    for provider in providers:
        if provider.name == name:
            return provider
    return None
