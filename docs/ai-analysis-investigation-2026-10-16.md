# AI 分析首字延迟与图片接入调查（2026-10-16）

客户端模型 `jyi-default` 由 JYI 路由到 OpenCode Go 的 DeepSeek V4.1 Flash。
文字与图片都使用 JYI `/v1/chat/completions` 和同一个模型别名；客户端按类别分批，
并没有另一个待配置的图片端口。

当前配置按用户要求保留并默认启用最高档推理：`thinking.type=enabled`、
`reasoning_effort=max`。常规请求预算为 131,072 token，网关按模型目录允许最高
384,000；推理与正文共同消耗此预算。此前关闭推理仅作为对照试验，已经撤回。

## 已确认的原因

1. 原 JYI 网关的 `apps/ai-platform/worker/gateway-estimate.ts` 固定把输出限制在
   16,384 token，并由 `apps/ai-gateway/worker/upstream.ts` 改写实际发往上游的
   `max_tokens`。模型目录原来公布的 384,000 是模型容量，不能代表网关实际限制。
2. DeepSeek 默认启用 high 推理。原日志中，请求了 384,000 token 的纯文字批次在
   16,384 completion token 处以 `length` 结束，只收到推理，没有正文。
3. 使用真实登录态发送单张测试图片，生产网关返回 HTTP 422 / `unsupported_content`：
   `Image and audio inputs are not supported on prepaid gateway requests yet.`
   这是网关尚未接入图片计量造成的拒绝，与本地图片能否解密是两个独立阶段。

## 客户端修复及实测

- 默认别名和明确的 V4.1 Flash 模型采用 `thinking.type=enabled`、
  `reasoning_effort=max`、`temperature=1.0`；文字、图片、合并和续写均沿用。
- 常规分析预算为 131,072，采用 DeepSeek 最高推理档的官方默认预算。
  网关允许显式请求最高 384,000，并按请求预算足额预留、按实际用量结算。
- SSE 错误直接报告，不把错误当作空正文再次发送付费请求。
- 日志补充实际请求预算、推理开关、响应模型、推理 token 数及网关请求 ID。
- 图片拒绝提示明确区分“图片已解密，但网关拒绝输入”；失败图片不计入成功消息数。

以下是此前的对照数据，不代表最高推理档的时延：对当前群历史中取出的同一批 38 条文字，low 推理运行 50 秒时已有
10,783 个推理字符，仍无正文；关闭推理后通过真实客户端调用链，首字约 2.28 秒，
55.24 秒完整结束，报告 21,576 字符、222 个表格行。这是延迟和完成性验证，
不代表逐行订单事实已经人工核对。

## 网关修复与线上验收

源码位置：`D:/Projects/Web/JYI`。

- 只在有明确视觉计量上限的 DeepSeek Flash 路由开放 inline JPEG/PNG/WebP。
- 每批最多 5 张，请求最大 8 MiB；其余文本和工具 schema 仍受 256 KiB 限制。
- Base64 仅计入传输大小；图片按每张最多 1,024 输入 token 预留，保留实际用量结算。
- 撤除对已知模型固定 16,384 的限制，按目录中的输出容量和上下文边界进行预留。
  当前默认模型目录输出容量为 384,000；没有容量元数据的模型仍保留保守回退限制。
- DeepSeek Flash 网关默认启用最高推理档，未指定输出预算时默认 128K。
- 修复 SSE 结算顺序：先结算实际用量再发送 `[DONE]`，并通过 `waitUntil` 保证后台结算任务完成。
- 52 项相关测试、四个相关 TS 项目的类型检查、定向 lint 已通过。

依据：[DeepSeek 视觉文档](https://api-docs.deepseek.com/guides/vision/)及
[思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)。

已发布 `jyi-ai-gateway`，当前版本 `6eb19c2e-9e64-4e0e-ae04-3084d385b2b5`。
初次图片接入版本为 `16b352f4-6c6e-4275-8435-a05a1c8f108b`。

使用客户端保存的登录态，通过真实 `_run_summary_jyi` 调用链验收；测试中的商品和数量
只写在临时生成的图片像素内，没有写入文件名、消息占位符或文字提示：

- 最高档推理单图：准确读出 `ORANGES 7 BOXES`，约 3.44 秒出现正文，3.85 秒完成。
  上游实际返回 259 reasoning token、347 completion token，`finish_reason=stop`，证实推理开启。
- 最高档推理 38 条文字：约 173.96 秒出现正文，208.02 秒完成，推理 31,889 token、
  总输出 42,156 token，正文 15,279 字符，表格 210 行，`finish_reason=stop`。
  推理本身已超过原来的 16,384 上限，仍正常产生正文。
  请求 `gwr_e71f00533f69449a8e65c932` 已通过状态接口确认 `settled`，实际结算
  128,470,000 credits；没有遗留本次请求的预留额度。
- 先前关闭推理的单图对照：约 2.10 秒出现正文，2.71 秒完成。
- 先前关闭推理的混合对照：1 条文字及 5 张图片，约 1.92 秒开始输出，7.79 秒完成文字、图片和最终合并。
  最终报告保留文字里的 PEARS，以及图片里的 ORANGES、APPLES、MILK、RICE、EGGS，
  成功消息数为 6，无错误或部分完成警告。
- 生产模型目录现在返回 `max_output_tokens=384000`、`max_images_per_request=5`、
  `input_modalities=[text,image]`。
- 本地客户端已重启并重新出现主窗口，加载了这次请求参数和错误处理修复。

当前账号无法一次预留 384K 请求，实测返回 402，所以常规预算采用 128K；这不改变
`reasoning_effort=max`。调查中发现已完成请求的预留遗留，结算顺序现已修复。
已按捕获到的最终 SSE 用量，通过原有 `settleRequest` 结算验证图片请求
`gwr_1f65eca41cb547a0a2b6d9aa`：623 prompt token、347 completion token，
302 upstream micro-USD，实际结算 1,510,000 credits，释放其余预留。
其他没有完整用量证据的历史请求没有据此自动退款或结算。

## 回归范围

新增客户端回归覆盖默认请求参数、图文同端点、续写、非流式回退、推理耗尽不重发、
SSE 错误及日志、图片失败提示。选定的客户端回归运行 73 项，除 1 项跳过外全部通过；
最终又确认新增的 10 项回归及取消分析回归通过。扩大运行原有测试还发现订单 schema fixture、旧的
9,000 字长度假设及文档分批假设对应的失败；这些路径未在本次修改。
最高推理配置、日志请求 ID 和取消流程的本轮 14 项回归已通过。
