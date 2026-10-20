# 只读边界：产品不替用户给微信发任何消息

## 一句话说清

产品对微信只做四件事：**读消息、整理、记单、导出**。不发消息、不加好友、
不自动回复。

## 为什么这条值得单独写一份

因为律师函来的时候，"这个工具不会替我给别人发微信"必须是一条**从二进制里能
验证的事实**，而不是文档里的一句承诺。

所以这条边界被拆成三道自动检查（`tests/test_read_only_boundary.py`）：

| 层 | 查什么 | 怎么红 |
| --- | --- | --- |
| 源码 | 产品代码不许 import 那套点击自动化（`WeChatAuto`：点"发送"、自动加好友、自动回复） | AST 扫 import |
| 源码 | 顶层不许再导出它（`__init__.py` 的懒加载是最大的坑：静态分析看不见，却能触发一次运行时 import） | 读 `_EXPORTS` 的值 |
| spec | `deploy/merchant.spec` 显式排除它和整个 `pywechat` 包 | 读 `EXCLUDES` |
| 制品 | 打出来的 exe 的 `PYZ` 目录里不许有它们 | 解包逐个模块名查 |

外加一条更细的：**进包的模块里不许碰聊天输入框**（`CurrentChatEdit`）。
唯一用到那个控件的功能是"在群里发接龙"（`utils.message_chain`），已经删掉。
剩下的按键操作都在**读**（点开消息、翻页、选成员、截屏比对）和**在搜索框里
打字**（公众号 / 视频号 / 小程序搜索）—— 那不是发消息。

## 以前为什么没出事

`WeChatAuto` 只被 `__init__.py` 的 `__getattr__` 懒加载指着，而 PyInstaller
只收**静态可见**的 import，所以它一直没进包。

这是"恰好"，不是"保证"：哪天有人为了加个功能 import 一下，发送能力就静悄悄
进包了。所以出口已经封死（不再导出），spec 里也显式排除了。

## 怎么自己验一遍

```powershell
# 源码 + spec 两道
.\.venv\Scripts\python.exe -m pytest tests\test_read_only_boundary.py -q

# 制品那道（要先构建过；没有产物会跳过）
#   装好 PyInstaller 后：
.\.venv\Scripts\python.exe -c "from PyInstaller.archive.readers import CArchiveReader; import pathlib; e=list(pathlib.Path('dist').glob('*/*.exe'))[0]; print([m for m in CArchiveReader(str(e)).open_embedded_archive('PYZ.pyz').toc if 'WeChatAuto' in m])"
```

最后那条应该打印 `[]`。

## 读消息走哪里

本机解密库直读（`localdb.py` / `wcdb.py`），**不经过任何服务器**。所以：

- 读消息是本地的，离线可用；
- 也因此，"吊销网关 key"只能停掉 AI 整理，**停不掉本地读消息** —— 这是这条
  设计的真实边界，别假装它不存在；
- 以及，"能调网关"和"能不能读你的微信"是两条**完全独立**的路。给网关加闸门
  （JYI 侧，见 JYI 仓库的 `docs/ai-application-approval.md`）对"防律师函"
  几乎没用 —— 有用的是分发管控和上面这条只读边界。

## 别把它改回去

要加新功能时，如果发现需要"发消息"（自动回复、群发、确认已读）：

1. 先确认这不是产品该做的事 —— 现在的形态是**给商户看订单**，不是替商户
   跟客户聊天；
2. 确实需要的话，那是**另一个产品**，另一个包，另一条边界 —— 别混进现在这个。
