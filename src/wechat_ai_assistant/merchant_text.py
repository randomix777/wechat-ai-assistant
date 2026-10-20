"""商户可见文案的**纯逻辑**：截断、占位、审计、数据来源说明。

这些函数原来长在 Tk 界面文件里（``merchant_ui.py``，已删）。它们和 Tk 一点关系
都没有 —— 是"商户看到什么"的规则，所以搬到这里：界面（现在是 Web 页面）只渲染，
规则只有一份。

搬家的理由很直接：它们**有测试盯着**（界面上不许出现技术词、长内容要能看出被截断、
"这是演示数据"必须如实说出来）。跟着界面文件一起删掉，就等于把这些底线一起删了。
"""
from __future__ import annotations

from typing import Any

#: 商家界面上允许出现的全部技术词，出现即视为回归。
FORBIDDEN_TERMS = ("token", "apikey", "api key", "provider", "session", "bearer",
                   "opencode", "model", "sqlite")

#: 表格一列最多显示多少字。超出的部分用省略号收尾 —— 不加省略号的话，
#: 商户看到的是一句**被拦腰切断**的话，看不出订单到底写了什么。
#:
#: 这个数字不是随手定的。用真实群里的消息量过：客户常把问题写在句子后半段，
#: 28 字会把「修改后对之前的订单数据有影响吗？」切成「…有影…」—— 问题本身
#: 正好被吃掉。40 字能盖住大多数一句完整的诉求。
CELL_LIMIT = 40

#: 非文字消息在核对页上的占位。
EMPTY_CONTENT_LABELS = {
    "image": "［图片］",
    "voice": "［语音］",
    "video": "［视频］",
    "file": "［文件］",
    "location": "［位置］",
    "transfer": "［转账］",
    "red_packet": "［红包］",
    "history": "［聊天记录］",
    "other": "［非文字消息］",
}


PRIVACY_TEXT = (
    "【谁在处理】本软件在您的电脑上运行；整理任务由您登录的产品账号对应的云端服务完成。\n\n"
    "【处理目的】把您主动选择的群消息整理成订单草稿或整理报告，供您核对。不用于其他用途。\n\n"
    "【仅在您点「开始整理」时，发送本次需要分析的内容】\n"
    "· 这一条待分析消息的内容和发送时间\n"
    "· 已绑定的客户身份（姓名、群内昵称）\n"
    "· 商品资料和单位规则\n"
    "· 该客户在该消息时间之前的近期订单和已确认的习惯\n"
    "· 该订单当前的状态\n\n"
    "【不会发送】您的微信聊天全库、微信登录状态、通讯录、支付信息，以及其他客户的数据。\n\n"
    "【在本机完成的部分】读取消息、解密数据库副本、保存订单和核对记录都在您的电脑上完成。"
    "读取使用的是微信数据库的一份只读副本，读完即删，原始数据库不会被修改。\n\n"
    "【保存期限】订单、消息和核对记录保存在本机数据目录，长期保留，您可以随时导出或删除。"
    "云端在本次整理完成后即删除提交的消息正文，只保留整理结论；任务使用独立临时会话，结束即清理。\n\n"
    "【您的权利】您可以随时导出全部数据、删除本机数据（退出软件后删除 data 目录），或退出产品账号。"
    "退出登录不会删除本机数据，但会立即断开云端能力。\n\n"
    "【安全措施】账号凭证使用 Windows 系统加密保存，换电脑无法解密，需要重新登录；"
    "与云端通信使用加密连接；本软件不收集与整理无关的个人信息。\n\n"
    "【变更与联系】本说明如有变更，会在软件内更新。有疑问请联系为您提供本软件的客服。"
)


def cell(text: Any, limit: int = CELL_LIMIT) -> str:
    """表格里的一格。

    真实群里的订单摘要常常很长（地址、备注、多行内容）。表格宽度固定，
    渲染层会直接切掉后面的字，而且**不留任何痕迹** —— 商户看到半句话，
    会以为订单就这么长。这里主动截断并加省略号，让"还有内容"是看得见的。

    被截掉的原文**不会丢**：行详情里能看到全文。
    """
    value = str(text if text is not None else "").replace("\n", " ").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def display_content(kind: Any, content: Any) -> str:
    """核对页内容格的显示文本。有字显示字，没字按类型给占位。"""
    text = str(content or "").strip()
    if text:
        return text
    return EMPTY_CONTENT_LABELS.get(str(kind or "").strip().lower(), "［非文字消息］")


def audit_text(text: str) -> list[str]:
    """商户可见文案里漏出来的技术词。空列表 = 干净。"""
    lowered = text.lower()
    return [term for term in FORBIDDEN_TERMS if term in lowered]


def merchant_labels(job_state: str, error_code: str = "") -> str:
    """云端任务状态的中文说法。

    界面和本地解析服务共用同一份，同一个状态就不会有两种说法。
    """
    from .client_login import status_label

    return status_label(job_state, error_code)


def data_origin_note(view: Any) -> str:
    """一句话说清屏幕上的东西**从哪来**。

    两件独立的事实，各说各的：

    * 消息是不是真的读到的（演示数据源 vs 真实微信）
    * 订单是不是模型分析出来的（真模型 vs 固定规则）

    都不成立时**什么都不显示** —— 正常使用时不必一直挂着一句警告，
    那会让真正的警告失去分量。
    """
    notes = []
    if getattr(view, "simulated", False):
        notes.append("读到的消息来自演示，不是真实微信里的内容")
    if not getattr(view, "model_executed", False):
        notes.append("订单不是模型分析出来的，是按固定规则整理的")
    if not notes:
        return ""
    return "数据来源：" + "；".join(notes)


__all__ = [
    "CELL_LIMIT", "EMPTY_CONTENT_LABELS", "FORBIDDEN_TERMS", "PRIVACY_TEXT",
    "audit_text", "cell", "data_origin_note", "display_content", "merchant_labels",
]
