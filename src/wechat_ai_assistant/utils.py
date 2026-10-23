"""发送人识别状态的占位串。

这里曾经是 UI 自动化（pywinauto 遍历聊天窗口）那套读取方案的解析器集合
（parse_chat_history / parse_group_chat_history / 头像点开认人 / 两次遍历对齐
等），随 WeChatAuto.py 一起删了：读取只剩解密数据库一条路（见 wcdb.py、
history_read.py），不再碰微信窗口。

留下来的只有「发送人认没认出来」的占位串：老的存量数据里还有它们，
读取进度和界面提示要靠 ``sender_is_resolved`` 把它们识别出来。
"""
from __future__ import annotations

#: 消息发送人没认出来时的占位串。历史由来：UIA 遍历拿不到发送人时只能写这句。
#: 现在解密库路径不再**写入**它，但存量行里还有，判断时必须认识。
UNRESOLVED_SENDER = "（发送人未识别）"
UNRESOLVED_TIMESTAMP = "（时间未识别）"

#: 旧字符串。**只用于识别历史数据，不用于写入。**
#: 已经落库的订单里可能留着它，判断「这条消息有没有认出发送人」时两个都要认。
LEGACY_UNRESOLVED_SENDER = "红包或转账(无法获取发送人)"
LEGACY_UNRESOLVED_TIMESTAMP = "红包或转账(无法获取时间戳)"


#: 发送人算不算「认出来了」。历史占位串一律算没认出来。
def sender_is_resolved(sender: str | None) -> bool:
    text = str(sender or "").strip()
    return bool(text) and text not in (UNRESOLVED_SENDER, LEGACY_UNRESOLVED_SENDER)
