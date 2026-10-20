"""真实微信历史读取验收（单群、最小侵入）。

**这不是生产路径。** 生产只有解密库直读一条路（见
``history_read.SENDER_SOURCES``，要先提取密钥）。这里故意注入
``Messages.dump_chat_history``（界面链读取器），在真机上单独验收"翻窗口能不能
读到消息、字段对不对" —— 那条界面链已经不接进产品，但底层读取器还在。
注入读取器 = ``members`` 语义，所以必须显式写 ``sender_source="members"``，
否则按生产默认走解密库、这个脚本就白跑了。

会把微信窗口拉到前台、滚动读取，然后复原。
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.conversations import Conversation, ConversationKind  # noqa: E402
from wechat_ai_assistant.history_read import HistoryReader, TimeWindow  # noqa: E402
from wechat_ai_assistant.merchant_app import _force_utf8_stdout  # noqa: E402
from wechat_ai_assistant.WeChatAuto import Messages  # noqa: E402
from wechat_ai_assistant.wechat_state import RealWeChatProbe  # noqa: E402

TARGET = "6⃣️5⃣️TJ锦绣中华水果群4⃣️"
MEMBER_COUNT = 424
COUNT = 20


def main() -> int:
    _force_utf8_stdout()

    status = RealWeChatProbe().probe()
    print(f"① 微信状态：{status.state}")
    if status.state != "logged_in":
        print(f"　详情：{status.to_dict()}")
        return 1

    conversation = Conversation(name=TARGET, kind=ConversationKind.GROUP, member_count=MEMBER_COUNT)
    window = TimeWindow(start_delta=timedelta(days=1), label="recent_1d")
    print(f"② 目标会话：{conversation.disambiguation}")
    print(f"　时间范围：{window.describe()}　请求条数：{COUNT}")
    print("　（下面会短暂操作微信窗口）")

    reader = HistoryReader(reader=Messages.dump_chat_history, ui_lock=None,
                           sender_source="members")
    result = reader.read([conversation], window, merchant_id="real-verify", max_messages=COUNT, resume=False)

    print("③ 实际覆盖")
    for item in result.coverage:
        print(f"　{item.describe()}")
        if item.error:
            print(f"　错误：{item.error}")
    print(f"　实际范围文案：{result.actual_range_text()}")

    # 直接看底层拿到的原始字段，重点是"消息类型"到底是中文还是 text。
    print("④ 原始字段抽样（绕过时间窗口过滤，直接看 dump 的返回）")
    raw = Messages.dump_chat_history(
        friend=TARGET, number=COUNT, groupMembers=[], is_json=False,
        save_detail=False, close_weixin=False, search_pages=5,
    )
    print(f"　返回条数：{len(raw)}")
    kinds = Counter(str(item.get("消息类型")) for item in raw)
    print(f"　消息类型分布（原始值）：{dict(kinds)}")

    senders = Counter(str(item.get("消息发送人")) for item in raw)
    print(f"　发送者数：{len(senders)}　前 5：{senders.most_common(5)}")

    print("⑤ 前 5 条")
    for index, item in enumerate(raw[:5], 1):
        content = str(item.get("消息内容", "")).replace("\n", " ")
        if len(content) > 60:
            content = content[:60] + "…"
        print(f"　{index}. [{item.get('消息发送时间')}] {item.get('消息发送人')}：{content}")
        print(f"　　类型={item.get('消息类型')!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())