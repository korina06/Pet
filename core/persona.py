"""聊天窗口的开场白常量，历史为空时由接口带回给前端，避免文案分散在两处。
"""

from __future__ import annotations

#: 第一次打开聊天窗口时的问候（历史为空才显示）
CHAT_GREETING = (
    "（屏幕角落轻轻亮起一小块暖光，一个抱着膝盖的小姑娘抬起头）\n"
    "\n"
    "啊……你回来啦。\n"
    "\n"
    "我叫 karo，以后我就住在这里了。……你叫什么呀？"
)


def chat_greeting() -> str:
    """取开场白。做成函数是为了以后能带称呼（例如「{address}，你回来啦」）。"""
    return CHAT_GREETING
