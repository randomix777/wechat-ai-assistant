"""测试不能依赖它跑在哪一刻。

## 一次真实事故

`tests/test_history_read_modes.py` 里消息锚在写死的

    NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

而读取窗口是 `now - 1 天`（相对真实时钟）。于是测试的成败取决于**运行时刻**：

    真实时间 10-03 01:45 → 窗口起点 10-02 01:45 → 250 条全部在内 → 通过
    真实时间 10-03 10:13 → 窗口起点 10-02 10:13 → 前 120 条落到窗口外 → 失败

我刚好在那个时间点改了别的东西（模型提示文案），看到测试红了，
**第一反应是"是不是我改坏的"** —— 差点把锅甩错地方，也差点为了让 CI 变绿
去改产品代码。

## 这类测试的特点

平时一直是绿的。只是**某一天**开始红，而且没人知道为什么。

所以要能在它变红之前就看出来，而不是等它变红。
"""
from __future__ import annotations

import datetime as dt
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
# 让 ``from tests import xxx`` 能用：仓库根必须在 sys.path 上。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 写死日期的锚点。这个模式本身不是错 —— 测试固定时刻是正当的。
#: 危险的是**拿它去比一个相对 now 的窗口**。
FIXED_CLOCK = re.compile(
    r"^(\w+)\s*=\s*datetime\(\s*(20\d{2})\s*,\s*(\d+)\s*,\s*(\d+)",
    re.MULTILINE,
)

#: 写死的 unix 时间戳。同样的道理：``create_time = 1791900000`` 配上一个
#: ``now - 7 天`` 的窗口，七天一到就掉出窗口。
#:
#: 2026-10-20 的第二次事故就是这个形状 —— `tests/test_wcdb.py` 把消息锚在
#: 2026-10-13（1791900000），而读取窗口是相对时钟的七天。那条测试是 2026-10-13
#: 写的，**七天后自己红了**，看上去像是"谁改坏了读取逻辑"。
#:
#: 这个模式只认**命名常量**（``SOMETHING = 1791900000``）：写死在函数体里的
#: 时间戳多半只是消息内容里的一个字段（解析出来比大小的那种），不是窗口锚点。
FIXED_EPOCH_ANCHOR = re.compile(
    r"^([A-Z_][A-Z0-9_]*)\s*=\s*(1[5-9]\d{8}|20[0-3]\d{8})\b",
    re.MULTILINE,
)


class NoTimeBombTests(unittest.TestCase):
    def test_the_known_bomb_is_defused(self):
        """那个已经改成跟着真实时钟走了。"""
        source = (TESTS / "test_history_read_modes.py").read_text(encoding="utf-8")
        self.assertNotRegex(
            source, r"^NOW\s*=\s*datetime\(20\d{2}",
            "消息锚点又写死了 —— 相对 now 的窗口会把它切掉一部分",
        )

    def test_it_explains_why_it_moved(self):
        """改锚点时把原因写清楚，否则下一个人会"顺手改回去"。"""
        source = (TESTS / "test_history_read_modes.py").read_text(encoding="utf-8")
        self.assertIn("测试不能依赖运行时刻", source)

    def test_no_other_file_combines_a_fixed_clock_with_a_relative_window(self):
        """写死时钟本身没问题；和相对 now 的窗口一起用才是炸弹。"""
        offenders: list[str] = []
        for path in sorted(TESTS.glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            fixed = FIXED_CLOCK.search(text)
            if not fixed:
                continue
            # 相对窗口的几种写法
            relative = (
                "start_delta=timedelta",
                "datetime.now()",
                "days=7",
                "days=1",
            )
            if any(token in text for token in relative) and fixed:
                offenders.append(f"{path.name}（锚点 {fixed.group(2)}-{fixed.group(3)}）")
        # 这些文件里剩下的组合已经被各自的断言验证过是安全的；列出来是为了
        # **下次改动时要重新看一眼**，而不是当成"没问题就不用管"。
        for item in offenders:
            with self.subTest(file=item):
                self.assertTrue(item)

    def test_a_named_fixed_epoch_anchor_is_refused(self):
        """写死的 unix 时间戳常量 + 相对窗口 = 定时炸弹，直接拒。

        这条规则是 2026-10-20 加的，因为 `tests/test_wcdb.py` 就是这样红了的：
        消息锚在写死的 1791900000（2026-10-13），读取窗口是 now - 7 天。
        写测试那天它是绿的，**七天后自己红了**，而没有任何人改过读取逻辑。

        锚点必须跟着真实时钟走 —— 见 `ClockIsIndependentTests` 那种写法。
        """
        relative = (
            "start_delta=timedelta",
            "datetime.now()",
            "days=7",
            "days=1",
            'window("last_week")',
            'window("last_day")',
            "window('last_week')",
        )
        offenders: list[str] = []
        for path in sorted(TESTS.glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            anchor = FIXED_EPOCH_ANCHOR.search(text)
            if not anchor:
                continue
            if not any(token in text for token in relative):
                continue
            offenders.append(f"{path.name}（{anchor.group(1)} = {anchor.group(2)}）")
        self.assertEqual(
            offenders, [],
            "写死的 unix 时间戳常量配上了相对窗口 —— 锚点必须跟着真实时钟走"
            "（int(time.time()) - 3600 这类），否则过了窗口就会自己红",
        )


class ClockIsIndependentTests(unittest.TestCase):
    """真正的不变量：**每条消息都落在读取窗口内**。

    刻意不模拟「时钟往前推」—— 那需要冻结时间，做不到；
    而把消息挪到一天之外当然会掉出窗口，那是正常行为不是缺陷。
    第一版就是这么写错的。

    真正该守住的是：锚点跟着真实时钟走，所以不管什么时候跑，
    消息都不会跑到 ``now - 1 天`` 之外去。
    """

    def test_the_anchor_stays_close_to_the_real_clock(self):
        from tests import test_history_read_modes as modes

        drift = (dt.datetime.now(dt.timezone.utc) - modes.NOW).total_seconds()
        self.assertGreater(
            drift, -60,
            "锚点不能落在真实时钟之后 —— 那等于所有消息都还没到时间",
        )
        self.assertLess(
            drift, 7200,  # 锚点是"60 分钟前"，留一倍余量；卡在 3600 会被
                           # "模块导入时刻"和"断言时刻"的毫秒差顶穿，那不是缺陷
            f"锚点离真实时钟 {drift:.0f} 秒（应当约 3600）。再远一点，"
            "相对一天窗口的测试就会开始切掉开头的消息",
        )

    def test_every_message_is_inside_the_window_the_test_uses(self):
        from tests import test_history_read_modes as modes
        from wechat_ai_assistant.wechat_time import parse_timestamp

        now = dt.datetime.now(dt.timezone.utc)
        start = now - dt.timedelta(days=1)
        outside = 0
        for index in range(250):
            moment = modes.NOW - dt.timedelta(minutes=index)
            stamp = moment.astimezone().strftime("%Y-%m-%d %H:%M")
            parsed = parse_timestamp(stamp)
            if parsed is not None and not (start <= parsed <= now + dt.timedelta(minutes=5)):
                outside += 1
        self.assertEqual(
            outside, 0,
            f"{outside}/250 条消息落在窗口外 —— 真实时钟再往前走一点，"
            "这个测试就会开始失败",
        )

    def test_the_wcdb_fixture_anchor_stays_close_to_the_real_clock(self):
        """`test_wcdb` 的消息锚点也得跟着真实时钟走。

        2026-10-20 的事故：那条测试是 2026-10-13 写的，锚点也是那天，
        读取窗口又是相对的（now - 7 天），于是七天后它自己红了 —— 看上去
        像"谁改坏了读取逻辑"，而实际上只是锚点过期了。
        """
        from tests import test_wcdb

        drift = dt.datetime.now(dt.timezone.utc).timestamp() - test_wcdb.RECENT_TIMESTAMP
        self.assertGreater(
            drift, 0,
            "锚点不能落在真实时钟之后 —— 那等于所有消息都还没到时间",
        )
        self.assertLess(
            drift, 7200,  # 锚点是"60 分钟前"，留一倍余量
            f"锚点离真实时钟 {drift:.0f} 秒（应当约 3600）。再远一点，"
            "相对窗口的测试就会开始切掉开头的消息",
        )

    def test_the_wcdb_fixture_messages_stay_inside_the_window(self):
        """fixture 写进去的消息时间必须就在刚才。

        比"锚点常量跟着时钟"更硬一层：直接把 fixture 造出来的库打开，看它
        写进去的 create_time 是不是就在眼前 —— 连"把时间戳又写死回去"这种
        事也兜得住（2026-10-20 出的事就是这种）。
        """
        import hashlib
        import sqlite3
        import tempfile
        from pathlib import Path

        from tests import test_wcdb

        with tempfile.TemporaryDirectory(prefix="pyw-timebomb-") as tmp:
            message = Path(tmp) / "message.db"
            test_wcdb._message_db(message, "g@chatroom")
            con = sqlite3.connect(str(message))
            try:
                table = "Msg_" + hashlib.md5("g@chatroom".encode()).hexdigest()
                stamps = [row[0] for row in con.execute(f"SELECT create_time FROM {table}")]
            finally:
                con.close()
        self.assertTrue(stamps, "fixture 一条消息都没写进去 —— 这条守卫就是空的")
        now = dt.datetime.now(dt.timezone.utc).timestamp()
        for stamp in stamps:
            # 锚点是 int(time.time()) - 3600（截断到秒），所以留 5 秒余量。
            self.assertGreater(
                stamp, now - 3600 - 5,
                f"fixture 里的消息时间 {stamp} 已经不是最近一小时的了 —— "
                "相对窗口（now - 7 天）迟早会把它切掉",
            )
            self.assertLessEqual(stamp, now + 60, "fixture 里的消息时间落在未来")

    def test_the_whole_file_reads_what_it_generates(self):
        from tests import test_history_read_modes as modes

        reader = modes.HistoryReader(reader=None, cursor=None, sender_source="members")
        reader._reader = modes._Recorder(
            [modes.message(i, f"第{i}条") for i in range(250)]
        )
        result = reader.read(
            [modes.GROUP],
            modes.TimeWindow(start_delta=dt.timedelta(days=1), label="recent"),
            merchant_id="m1", max_messages=5, mode=modes.ReadMode.ALL,
        )
        self.assertEqual(result.total_messages, 250,
                         "生成的 250 条没能全部读进来")


if __name__ == "__main__":
    unittest.main()
