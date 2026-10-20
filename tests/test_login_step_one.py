"""登录挡在流程前面（**前置门槛**），而且每次启动都停在它上面。

## 由来

商户原话：**「登录放在第一步是不是更合理，第四步显示无模型」** —— 登录入口以前
只在第 4 步，商户一路走到"智能整理"才发现登不上；更糟的是那屏写着
「免费模型暂不可用」，归错了因。后来挪到第 1 步，再后来商户要求
**「登录改成第 0 步」**：它是所有事情的前提，登不上就不放行。

## 这组测试盯什么

1. 登录页**不占流程编号**（步骤条从「检查微信」开始），但门禁的**判据在向导里**
   （``Wizard.advance``），不放行要说清差什么；
2. 起点对谁都一样是登录页：账号登着、以前走完过，都不改落点（已登录时那一屏
   写「已经登录」+「下一步」，不白挡一道）；
3. 界面上不许说"免费模型 / 不扣额度"这类产品里不存在的东西。

（界面已经统一到 Web；页面的文案审计在 ``test_web_wizard_flow.py`` 里对
``/api/state`` 的契约做。）
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


class _Store:
    """最小设置存储：向导只用到 load/save。"""

    def load(self):
        from wechat_ai_assistant.runmode import MerchantSettings

        return MerchantSettings()

    def save(self, _settings):
        pass


def _GateWizard(logged_in: bool):
    """只带登录门禁的向导，用来验登录门槛的放行判据。

    ``login_state`` 是可变开关：测试里翻一下就等于"刚在浏览器里登录成功"。
    """
    from wechat_ai_assistant.wizard import Wizard

    state = {"logged_in": logged_in}
    wizard = Wizard(
        _Store(), None, None, None, tenant="t",
        is_product_logged_in=lambda: state["logged_in"],
    )
    wizard.login_state = state
    # 和真实入口一样：界面起来时先定起始步（账号是界面之前恢复的）。
    wizard.resume()
    return wizard


class LoginIsAFrontGateTests(unittest.TestCase):
    def test_login_is_a_front_gate_not_a_step(self):
        """登录页有标题，但不在步骤条上、不占编号（编号从「检查微信」开始）。"""
        from wechat_ai_assistant.wizard import WizardStep

        self.assertNotIn(WizardStep.LOGIN, WizardStep.ORDER)
        self.assertEqual(WizardStep.ORDER[0], WizardStep.WECHAT)
        self.assertEqual(WizardStep.page_title(WizardStep.LOGIN), "登录产品账号",
                         "登录页不编号，标题就是它自己")
        self.assertEqual(WizardStep.TITLES[WizardStep.WECHAT], "检查微信")

    def test_the_gate_is_hard_and_everyone_starts_on_the_login_page(self):
        """门禁是硬的；起点对谁都一样是登录页。

        反转记录：``first_step`` 被改成过"已登录就直接跳到「检查微信」"，于是
        同一个软件每次打开停的地方都不一样 —— 取决于账号是什么时候恢复的。
        商户要的是每次打开都从头看一眼；已登录时那一屏写着「已经登录」，
        点一下「下一步」就往下走，不白挡一道。
        """
        from wechat_ai_assistant.wizard import WizardStep

        wizard = _GateWizard(logged_in=False)
        self.assertEqual(wizard.first_step(), WizardStep.LOGIN)
        outcome = wizard.advance()
        self.assertFalse(outcome["ok"], "没登录不许往前走")
        self.assertIn("登录", outcome["reason"], "要说清差什么，不能只说不行")

        wizard.login_state["logged_in"] = True
        self.assertEqual(wizard.first_step(), WizardStep.LOGIN,
                         "已登录也停在登录页 —— 那一屏会写「已经登录」，点一下就往下走")
        self.assertTrue(wizard.advance()["ok"])
        self.assertEqual(wizard.step, WizardStep.WECHAT)

    def test_the_page_carries_the_gate_but_not_explanatory_copy(self):
        """门禁靠**功能**表达，不靠文案解释。

        反转记录：这一条原来断言登录页正文里必须有「智能整理」那句话，用文字
        说明"不登录不许往下走、登录买到的是什么"。商户要求这一屏只留功能性
        内容（状态、额度、按钮），说明性长句全删 —— 所以现在钉的是功能契约：
        未登录时有一个明确的「登录产品账号」按钮，「下一步」的灰/亮只认服务端
        门禁（``data.product_gate_open``），页面不自己再判一次。
        """
        page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")
        body = page[page.index("  login(data)"):page.index("  wechat(data)")]
        # 只看**会渲染出去**的行：注释里提到这些词（讲为什么删）不算违规，
        # 否则改注释就会把测试弄红 —— 断言源码时先剥注释，别剥人话。
        code = "\n".join(line for line in body.splitlines()
                         if not line.strip().startswith("//"))
        # 功能性：一个明确的登录入口（未登录时）。
        self.assertIn('data-action="login"', code)
        self.assertIn("立即登录", code, "按钮上要有能认出来的动作词")
        # 功能性：「下一步」的灰/亮只认服务端门禁。
        self.assertIn("disabled: !data.product_gate_open", code)
        # 非功能性说明不许再回到这一屏。
        for phrase in ("不需要登录", "断网也能用", "登录之后「下一步」才能点"):
            self.assertNotIn(phrase, code, f"登录页不该再有说明性文案：{phrase}")

    def test_the_login_screen_carries_no_taglines_or_reassurance(self):
        """登录屏只留「一个动作」：标题 + 按钮。

        又清掉几处（都只说明，不带状态也不带动作）：

        * 页眉标语「会话整理与智能分析」—— 装饰；
        * 「登录后即可继续配置微信，并选择需要整理的会话。」—— 讲的是点了会怎样，
          而按钮本身就说明了这件事；
        * 「登录用于验证产品账号与服务权限。」—— 给商户解释"为什么必须登录"，
          按这一屏的规矩属于"读成这段不用管"的话；
        * 卡片上的小标签「账号验证」—— 和下面的标题「登录产品账号」同一个意思。

        判据：整页（剥掉注释）不含这些串，且功能性内容一条都没被顺手删掉。
        """
        page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")
        code = "\n".join(line for line in page.splitlines()
                         if not line.strip().startswith("//"))
        for gone in ("会话整理与智能分析", "登录后即可继续配置微信",
                     "登录用于验证产品账号与服务权限", "账号验证",
                     "brand-caption", "login-prompt", "login-footnote", "login-kicker"):
            with self.subTest(removed=gone):
                self.assertNotIn(gone, code, f"这条非功能性文案又回来了：{gone}")
        # 功能性内容一条都不许被顺手删掉。
        self.assertIn("立即登录", code)
        self.assertIn("已经登录：", code)
        self.assertIn("每个角色各自记进度", code, "计费口径是功能性的，不能删")


class WrongCauseTests(unittest.TestCase):
    """没登录不能说成"模型不可用" —— 商户会去反复刷新模型列表，而问题不在模型上。"""

    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "src" / "wechat_ai_assistant" / "application.py").read_text(
            encoding="utf-8")

    def test_not_logged_in_says_not_logged_in(self):
        start = self.source.index("def model_notice")
        block = self.source[start:start + 900]
        self.assertIn("还没有登录产品账号", block,
                      "未登录时必须说没登录，不能说模型不可用")

    def test_the_notice_checks_login_before_the_catalog(self):
        start = self.source.index("def model_notice")
        block = self.source[start:start + 900]
        self.assertLess(
            block.index("self.client is None"), block.index("free_available"),
            "必须先判登录，再判模型 —— 否则登录问题会被写成模型问题",
        )


class NoFreeModelPromiseTests(unittest.TestCase):
    """产品里**没有免费模型**，界面不许这么说；也不许承诺"不扣额度"。"""

    #: 会出现在商户屏幕上的文案所在文件（现在是 Web 那套）。
    MERCHANT_VISIBLE = ("web_wizard_page.py", "web_page.py", "web_server.py")

    def _merchant_text(self, name: str) -> str:
        """只取**会显示给商户的字面量**（跳过 docstring 与注释）。"""
        import io
        import tokenize

        source = (ROOT / "src" / "wechat_ai_assistant" / name).read_text(encoding="utf-8")
        out: list[str] = []
        prev = ""
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.STRING:
                at_start = prev.strip() in ("", ":") or prev.rstrip().endswith(('"""', "'''"))
                if not at_start:
                    out.append(tok.string)
            elif tok.type in (tokenize.FSTRING_START, tokenize.FSTRING_MIDDLE):
                out.append(tok.string)
            if tok.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                                tokenize.INDENT, tokenize.DEDENT):
                prev = tok.string
        return "\n".join(out)

    def test_no_merchant_screen_says_免费模型(self):
        for name in self.MERCHANT_VISIBLE:
            with self.subTest(file=name):
                self.assertNotIn("免费模型", self._merchant_text(name),
                                 f"{name} 里有商户能看到的「免费模型」")

    def test_no_merchant_screen_promises_不扣额度(self):
        for name in self.MERCHANT_VISIBLE:
            with self.subTest(file=name):
                self.assertNotIn("不扣额度", self._merchant_text(name),
                                 f"{name} 里有商户能看到的「不扣额度」")

    def test_the_backend_may_keep_the_mechanism(self):
        """网关侧的免费额度机制是**运营可配的能力**，不该因为界面不显示就删掉。"""
        gateway = (ROOT / "src" / "wechat_ai_assistant" / "gateway.py").read_text(
            encoding="utf-8")
        self.assertIn("no_free_model", gateway,
                      "网关的错误码是接口约定，不该被界面改动牵连")


if __name__ == "__main__":
    unittest.main()
