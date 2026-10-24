"""商户版 Web 向导页（单文件，字符串常量）—— 从登录到最后一步。

## 为什么是字符串而不是静态资源

和 ``web_page.py`` 同一个理由：打包成桌面程序时静态资源要多走一步（收集进包、
写路径解析）。一页字符串、零依赖，不会因为忘了打包某个文件而在别人机器上白屏。

## 这份页面只做一件事：渲染 `/api/state`

流程事实（步骤编号、标题、短名、能不能往下走）**全部由服务端给**：

* ``state.steps`` —— 步骤条和屏标题要的 ``number/title/short/state``，
  页面不写"第 N 步"
* ``state.product_gate_open`` —— 门禁，和 Tk 的登录页同一个判据
* ``state.wechat`` —— 微信状态的中文标题/说明/按钮，和 Tk 同一份文案

所以改流程、改文案、加一步都**不用动这个文件**。页面里唯一"硬"的东西是
「这一步有哪些按钮」—— 那是交互，不是流程事实。

## 现在跑在哪

开发期由 ``web_dev`` 挂在 ``/wizard``（浏览器里调，保存即刷新）；交付期由
pywebview 装壳，后端仍然是同一个 Python 进程。
"""

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>威信AI智能助理 __VERSION__</title>
<style>
  :root {
    --ink:#182230; --muted:#657487; --line:#e2e8ee; --bg:#f4f7f9; --card:#fff;
    --brand:#0e9f6e; --brand-dark:#0b7a54; --warn:#a8590b; --bad:#b4232c;
    --radius:12px;
  }
  * { box-sizing: border-box; }
  body { margin:0; width:100%; height:100vh; height:100dvh; overflow:hidden;
         display:grid; grid-template-columns:248px minmax(0,1fr);
         grid-template-rows:60px minmax(0,1fr) auto;
         font:14px/1.6 "Microsoft YaHei","PingFang SC",system-ui,sans-serif;
         color:var(--ink); background:linear-gradient(180deg,#f8fafc 0%,var(--bg) 42%); }
  header { flex:0 0 auto; background:var(--card); border-bottom:1px solid var(--line);
           min-height:60px; padding:10px max(20px,calc((100vw - 1160px) / 2));
           display:flex; align-items:center; gap:12px; position:relative; z-index:6;
           box-shadow:0 1px 3px rgba(24,34,48,.035); grid-column:1 / -1; grid-row:1; }
  .brand { display:flex; align-items:center; gap:10px; min-width:0; }
  .brand-mark { width:34px; height:34px; flex:0 0 34px; display:grid; place-items:center;
                color:#fff; border-radius:11px;
                background:linear-gradient(145deg,#18b582 0%,#07865e 100%);
                box-shadow:0 3px 8px rgba(14,159,110,.2); }
  .brand-mark svg { width:21px; height:21px; }
  header h1 { font-size:15px; font-weight:700; letter-spacing:.01em; margin:0; white-space:nowrap; }
  header .spacer { margin-left:auto; }
  .badge { font-size:12px; color:var(--muted); border:1px solid var(--line);
           background:#fff; border-radius:999px; padding:3px 10px; white-space:nowrap; }
  .badge#conn { display:inline-flex; align-items:center; gap:7px; }
  .badge#conn::before { content:""; width:7px; height:7px; border-radius:50%;
                        background:#94a3b3; }
  .badge#conn[data-kind="error"] { color:var(--bad); border-color:#f2c7c4; background:#fff7f6; }
  .badge#conn[data-kind="error"]::before { background:var(--bad); }
  .badge#conn[hidden] { display:none; }
  /* 步骤条：圆点 + 短名。数据来自 state.steps，这里只管画。 */
  #stepper { grid-column:1; grid-row:2 / 4; min-height:0; overflow-y:auto;
             display:flex; flex-direction:column; align-items:stretch; gap:5px;
             padding:24px 14px; background:#fff; border-right:1px solid var(--line); }
  #stepper::before { content:"办理流程"; padding:0 10px 12px; color:var(--muted);
                     font-size:12px; font-weight:700; letter-spacing:.04em; }
  .step { position:relative; z-index:1; display:grid; grid-template-columns:24px minmax(0,1fr);
          align-items:center; gap:10px; width:100%; min-height:42px;
          padding:8px 10px; border-radius:9px; white-space:nowrap; }
  .dot { width:24px; height:24px; border-radius:50%; display:inline-flex;
         align-items:center; justify-content:center; font-size:12px; font-weight:700;
         background:#e3e7ec; color:var(--muted); }
  .step.done .dot { background:var(--brand); color:#fff; }
  .step.current { background:#eaf6f1; }
  .step.current .dot { background:var(--brand); color:#fff;
                       box-shadow:0 0 0 4px rgba(14,159,110,.12); }
  .step.current .name { color:var(--brand-dark); font-weight:700; }
  .step .name { font-size:13px; color:var(--muted); }
  .step.done .name { color:var(--ink); }
  .arrow { position:relative; z-index:0; display:block; flex:0 0 12px; width:24px;
           height:12px; margin:-5px 0 -5px 10px; font-size:0; }
  .arrow::before { content:""; position:absolute; top:0; bottom:0; left:11px;
                   width:1px; background:var(--line); }
  main { flex:1 1 auto; min-height:0; overflow-y:auto; overscroll-behavior:contain;
         grid-column:2; grid-row:2; display:flex; flex-direction:column; align-items:center;
         min-width:0; width:100%; margin:0; padding:clamp(28px,5vh,56px) clamp(22px,3vw,48px); }
  footer { background:var(--card); border-top:1px solid var(--line);
           grid-column:2; grid-row:3; position:relative; z-index:5;
           padding:10px 0 calc(10px + env(safe-area-inset-bottom, 0px));
           box-shadow:0 -4px 18px rgba(24,34,48,.07); }
  footer .inner { width:100%; max-width:1080px; margin:0 auto; padding:0 clamp(22px,3vw,48px);
                  display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
  /* 刷新按钮那一行**不吸顶**：它是一次性维护动作，不需要一直挂在屏幕上，
     挂着只会从会话列表里截走一块可用高度。 */
  .conv-header { margin:0 -16px; padding:8px 16px 0; }
  /* 分类页签和"已选 N 个"吸顶：290 个会话往下翻时还要能切换分类、看选了几个。 */
  .conv-sticky { position:sticky; top:0; z-index:4; margin:0 -16px;
                 padding:0 16px; background:var(--card);
                 border-bottom:1px solid var(--line); }
  .conv-toolbar { min-height:34px; gap:10px; }
  .refresh-button { min-width:112px; display:inline-flex; align-items:center;
                    justify-content:center; gap:8px; }
  .refresh-button:disabled { opacity:.9; color:var(--muted); cursor:wait; }
  .conv-refresh-status { display:inline-flex; align-items:center; gap:7px;
                         min-height:20px; color:var(--muted); font-size:12px; }
  .conv-refresh-status[data-kind="success"] { color:var(--brand-dark); }
  .conv-refresh-status[data-kind="error"] { color:var(--bad); }
  .spinner { width:14px; height:14px; border:2px solid currentColor;
             border-right-color:transparent; border-radius:50%;
             animation:spin .75s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spinner { animation:none; } }
  .tabs { display:flex; gap:6px; margin:10px 0 0; border-bottom:1px solid var(--line); }
  button.tab { display:inline-flex; align-items:center; gap:7px; margin-bottom:-1px;
               border:0; border-bottom:2px solid transparent; border-radius:6px 6px 0 0;
               padding:8px 13px; color:var(--muted); background:transparent; }
  button.tab:hover:not(:disabled) { background:#f5f7f9; border-bottom-color:var(--line); }
  button.tab.active { color:var(--brand-dark); font-weight:700; border-bottom-color:var(--brand); }
  .tab-count { min-width:20px; padding:0 6px; border-radius:999px; background:#edf1f4;
               color:var(--muted); font-size:11px; line-height:20px; text-align:center; }
  button.tab.active .tab-count { background:#e6f5ef; color:var(--brand-dark); }
  .conv-selection-actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap;
                            padding:8px 0; }
  .conv-scope { color:var(--muted); margin-right:auto; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
          padding:24px; box-shadow:0 8px 28px rgba(24,34,48,.045); }
  #banner { width:min(100%,1080px); }
  #page { width:min(100%,1080px); margin-block:auto; }
  h2 { font-size:19px; line-height:1.4; font-weight:700; margin:0 0 16px; }
  .hint { color:var(--muted); font-size:12px; margin:8px 0 0; }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  button { font:inherit; min-height:38px; padding:7px 14px; border-radius:8px;
           border:1px solid var(--line); background:var(--card); color:var(--ink);
           cursor:pointer; transition:background-color .14s ease,border-color .14s ease,
           box-shadow .14s ease,transform .14s ease; }
  button:hover:not(:disabled) { border-color:#b9c3cf; box-shadow:0 2px 7px rgba(24,34,48,.07); }
  button:active:not(:disabled) { transform:translateY(1px); }
  button:focus-visible { outline:3px solid rgba(14,159,110,.22); outline-offset:2px; }
  button.primary { background:var(--brand); border-color:var(--brand); color:#fff;
                   box-shadow:0 2px 5px rgba(14,159,110,.16); }
  button.primary:hover:not(:disabled) { background:var(--brand-dark); border-color:var(--brand-dark); }
  button.big { padding:10px 18px; font-size:15px; }
  button:disabled { opacity:1; color:#8995a1; background:#eef1f3; border-color:#e4e8ec;
                    box-shadow:none; cursor:not-allowed; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; }
  .msg { padding:9px 12px; border-radius:6px; margin-bottom:10px; font-size:13px; }
  .msg.bad { background:#fdeceb; color:var(--bad); }
  .msg.good { background:#e8f6ee; color:var(--brand-dark); }
  .msg.warn { background:#fdf3e6; color:var(--warn); }
  .muted { color:var(--muted); }
  .empty { color:var(--muted); padding:10px 0; }
  .counts { gap:14px; margin:10px 0; }
  .count { font-size:13px; color:var(--muted); }
  .count b { font-size:18px; color:var(--ink); margin-right:4px; }
  .role-section { margin:0 0 14px; }
  .role-heading { display:flex; align-items:baseline; justify-content:space-between;
                  flex-wrap:wrap; gap:4px 12px; }
  .role-heading h3 { margin:0; font-size:14px; }
  .role-heading p { margin:0; color:var(--muted); font-size:12px; }
  .role-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,248px),1fr));
               gap:12px; margin-top:10px; }
  .pick-role { position:relative; display:flex; flex-direction:column; align-items:stretch;
               gap:10px; min-height:148px; padding:15px 16px; border:1px solid var(--line);
               border-radius:11px; background:var(--card); cursor:pointer;
               transition:border-color .14s ease,background-color .14s ease,box-shadow .14s ease; }
  .pick-role:hover { border-color:#b9cfc4; box-shadow:0 3px 10px rgba(24,34,48,.06); }
  .pick-role:has(input:checked) { border-color:#72c5a3; background:#f3fbf7;
                                  box-shadow:0 0 0 2px rgba(14,159,110,.09); }
  .pick-role:focus-within { outline:3px solid rgba(14,159,110,.2); outline-offset:2px; }
  .role-top { display:flex; align-items:center; justify-content:space-between; gap:12px; }
  .role-name { font-size:14px; line-height:1.4; font-weight:700; }
  .pick-role input { width:17px; height:17px; margin:0; flex:0 0 17px;
                     accent-color:var(--brand); }
  .role-description { flex:1; color:var(--muted); font-size:12px; line-height:1.55; }
  .role-mode { align-self:flex-start; display:inline-flex; align-items:center; min-height:23px;
               padding:2px 8px; border-radius:999px; font-size:11px; line-height:1.4; }
  .uses-ai { color:var(--brand-dark); background:#e6f5ef; }
  .no-ai { color:#596879; background:#eef1f4; }
  .role-result-hero { padding:20px 22px; border:1px solid #d8ebe2; border-radius:12px;
                      background:linear-gradient(125deg,#f0faf5,#fff 76%); }
  .role-result-kicker { color:var(--brand-dark); font-size:12px; font-weight:700; }
  .role-result-hero h3 { margin:4px 0 5px; font-size:18px; }
  .role-result-hero p { margin:0; color:var(--muted); font-size:13px; }
  .role-section-chips { display:flex; flex-wrap:wrap; gap:7px; margin-top:14px; }
  .role-section-chip { padding:3px 9px; border:1px solid #d8ebe2; border-radius:999px;
                       background:#fff; color:#426556; font-size:11px; }
  .analysis-live { margin:14px 0; padding:14px 16px; border:1px solid #d7e7df;
                   border-radius:10px; background:#fbfefc; }
  .analysis-live-head { display:flex; align-items:center; gap:8px; margin-bottom:9px;
                        color:var(--brand-dark); font-size:12px; font-weight:700; }
  .analysis-live-dot { width:7px; height:7px; border-radius:50%; background:var(--brand);
                       box-shadow:0 0 0 3px rgba(14,159,110,.12); }
  .analysis-live-text { max-height:320px; overflow:auto; margin:0; color:#344253;
                        font-family:inherit; font-size:13px; line-height:1.7;
                        white-space:pre-wrap; word-break:break-word; }
  .analysis-live-text.waiting { color:var(--muted); }
  /* 分析进行中独占一屏：正文给足高度，一眼就看得出字在往外冒。
     以前它只是"排在角色卡前面"的一块，窗口一矮就被顶出首屏。 */
  .analysis-live-full { margin:0; padding:18px 20px; }
  .analysis-live-full .analysis-live-head { font-size:13px; }
  .analysis-live-role { margin-left:auto; color:var(--muted); font-size:12px; font-weight:600; }
  .analysis-live-full .analysis-live-text { max-height:none; height:clamp(200px,46vh,460px); }
  .analysis-live-progress { display:flex; align-items:center; gap:9px; margin:6px 0 0;
                            color:#344253; font-size:13px; }
  /* 分析中：左右两栏（借控制台 console.py 那套 .grid2 分栏）。
     左边流式正文，右边这次要盯的类别；窄窗口自动叠成一栏。 */
  .analysis-split { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr);
                    gap:14px; align-items:start; margin:0 0 10px; }
  .analysis-side { background:var(--card); border:1px solid var(--line);
                   border-radius:10px; padding:14px 16px; min-width:0; }
  .analysis-side h4 { margin:0 0 8px; font-size:13px; }
  .analysis-wait { margin:0 0 8px; color:var(--muted); font-size:12px;
                   min-height:18px; }
  .analysis-reasoning { margin:0 0 8px; padding:6px 10px; border:1px dashed var(--line);
                         border-radius:8px; background:#fbfaf6; }
  .analysis-reasoning summary { font-size:12px; color:var(--muted); }
  .analysis-reasoning pre { margin:6px 0 0; max-height:180px; overflow:auto;
                             white-space:pre-wrap; word-break:break-word;
                             font:12px/1.65 inherit; color:#5b6775; }
  /* 数据与权限说明：默认折起来，商户要看就点开 —— 披露要在一眼能找到的地方，
     但不能变成每次都横在读取按钮前面的说明墙。 */
  .disclosure { margin:10px 0 0; padding:6px 10px; border:1px dashed var(--line);
                border-radius:8px; background:#fbfaf6; }
  .disclosure summary { font-size:12px; color:var(--muted); }
  .disclosure p { margin:6px 0 0; font-size:12px; line-height:1.7; color:#5b6775; }
  .analysis-sections { display:flex; flex-direction:column; gap:8px; }
  .analysis-section { border:1px solid var(--line); border-radius:9px;
                      padding:8px 11px; background:#fff; }
  .analysis-section.has-content { border-color:#cfe6db; background:#f7fbf9; }
  .analysis-section h5 { display:flex; align-items:baseline; justify-content:space-between;
                         gap:8px; margin:0 0 4px; font-size:12px; }
  .analysis-section h5 .muted { font-size:11px; font-weight:400; }
  .analysis-section pre { margin:0; white-space:pre-wrap; word-break:break-word;
                           font:12px/1.65 inherit; color:#344253; max-height:150px;
                           overflow:auto; }
  @media (max-width:900px) {
    .analysis-split { grid-template-columns:minmax(0,1fr); }
  }
  .result-stat { display:flex; align-items:baseline; gap:8px; margin:14px 0;
                 color:var(--muted); font-size:13px; }
  .result-stat strong { color:var(--ink); font-size:22px; }
  .ai-report { margin-top:14px; padding:16px; border:1px solid var(--line); border-radius:10px;
               background:#fff; }
  .ai-report-head { display:flex; align-items:center; justify-content:space-between;
                    gap:12px; margin-bottom:12px; }
  .ai-report-head strong { font-size:14px; }
  .ai-report-head span { color:var(--muted); font-size:12px; }
  .ai-report-content { color:#344253; font-size:13px; line-height:1.75; }
  .ai-report-content h4 { margin:16px 0 5px; color:var(--ink); font-size:13px; }
  .ai-report-content p { margin:5px 0; white-space:pre-wrap; }
  .ai-report-content ul { margin:5px 0 8px; padding-left:20px; }
  .ai-report-content li { padding:2px 0; }
  .file { display:inline-block; padding:7px 14px; border:1px solid var(--line);
          border-radius:6px; cursor:pointer; background:var(--card); }
  .file:hover { border-color:#b9c3cf; }
  a.file { color:var(--ink); text-decoration:none; }
  table.clickable tr[data-order] { cursor:pointer; }
  table.clickable tr[data-order]:hover { background:#fbfcfd; }
  pre.full { white-space:pre-wrap; word-break:break-word; margin:6px 0 0; font:inherit; }
  pre.logs { max-height:260px; overflow:auto; font:12px/1.5 Consolas,monospace;
             background:#1e2530; color:#d6e0ee; border-radius:6px; padding:10px;
             white-space:pre-wrap; }
  summary { cursor:pointer; font-weight:600; }
  h3 { font-size:14px; margin:14px 0 6px; }
  .bar { height:8px; background:#e3e7ec; border-radius:999px; overflow:hidden; }
  .bar > i { display:block; height:100%; background:var(--brand); width:0; }
  .quota-panel { margin:0 0 20px; padding:14px 16px; border:1px solid #dcebe4;
                 border-radius:12px; background:linear-gradient(120deg,#f5fbf8,#fff 72%); }
  .quota-head { display:flex; align-items:center; justify-content:space-between; gap:16px; }
  .quota-headline { min-width:0; margin:0; font-weight:600; }
  .quota-action { flex:0 0 auto; min-height:34px; padding:5px 12px; color:var(--brand-dark);
                  border-color:#c8e6d8; background:#fff; }
  .quota-panel .bar { margin-top:12px; }
  .quota-detail { margin:8px 0 0; color:var(--muted); font-size:12px; }
  .list { margin:6px 0 0; padding-left:18px; }
  @media (max-width:900px) {
    body { grid-template-columns:minmax(0,1fr);
           grid-template-rows:auto auto minmax(0,1fr) auto; }
    header { grid-column:1; grid-row:1; padding:10px 18px; }
    #stepper { grid-column:1; grid-row:2; min-height:0; flex-direction:row;
               align-items:center; gap:8px; padding:10px 18px; overflow-x:auto;
               overflow-y:hidden; border-right:0; border-bottom:1px solid var(--line); }
    #stepper::before { display:none; }
    .step { display:flex; flex:0 0 auto; width:auto; min-height:0; gap:7px;
            padding:0; border-radius:0; background:transparent; }
    .step.current { background:transparent; }
    .step.current .name { color:var(--ink); }
    .arrow { flex:0 0 auto; width:auto; height:auto; margin:0; }
    .arrow::before { display:none; }
    .arrow::after { content:"›"; color:#b9c3cf; font-size:14px; }
    main { grid-column:1; grid-row:3; max-width:none; padding:24px clamp(18px,4vw,36px); }
    footer { grid-column:1; grid-row:4; }
    footer .inner { max-width:1080px; padding:0 clamp(18px,4vw,36px); }
  }
  @media (max-width:760px) {
    header { min-height:54px; padding:9px 14px; gap:8px; }
    header h1 { font-size:14px; }
    #stepper { justify-content:flex-start; gap:8px; padding:10px 14px; }
    main { max-width:none; padding:16px 14px 24px; }
    .card { padding:18px; }
    footer .inner { padding:0 14px; }
  }
  @media (max-width:480px) {
    header { flex-wrap:wrap; }
    header .spacer { flex:1 0 8px; }
    .badge { padding:2px 8px; }
    footer .inner { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); }
    .quota-head { align-items:flex-start; gap:10px; }
    .quota-action { min-height:32px; padding:4px 9px; }
  }
  @media (prefers-reduced-motion: reduce) {
    button, .pick-role { transition:none; }
  }
  /* 未登录首屏聚焦唯一任务：登录。流程导航与操作栏在进入向导后再出现。 */
  body.login-screen { grid-template-columns:minmax(0,1fr);
                      grid-template-rows:64px minmax(0,1fr);
                      background:
                        radial-gradient(ellipse at 50% 38%,rgba(216,244,232,.62),transparent 46%),
                        linear-gradient(145deg,#f7fafc 0%,#f1f6f5 52%,#f6f8fb 100%); }
  body.login-screen #stepper,
  body.login-screen #footer { display:none; }
  body.login-screen header { min-height:64px; background:rgba(255,255,255,.78);
                             border-bottom-color:rgba(226,232,238,.76); box-shadow:none;
                             backdrop-filter:blur(12px); }
  body.login-screen .brand-mark { width:36px; height:36px; flex-basis:36px; border-radius:12px; }
  body.login-screen header .badge#ver { margin-left:auto; background:rgba(255,255,255,.7); }
  body.login-screen main { grid-column:1; grid-row:2; justify-content:center;
                           padding:clamp(28px,7vh,76px) 20px; }
  body.login-screen #banner { width:min(100%,480px); }
  body.login-screen #page { position:relative; width:min(100%,480px); margin:auto; padding:42px;
                            overflow:hidden; border-color:rgba(222,230,235,.9); border-radius:20px;
                            box-shadow:0 24px 70px rgba(27,52,44,.09),0 3px 10px rgba(27,52,44,.035); }
  body.login-screen #page::before { content:""; position:absolute; inset:0 0 auto; height:3px;
                                    background:linear-gradient(90deg,#10a878,#7bd5ad 58%,#c5eed9); }
  body.login-screen #page h2 { margin-bottom:9px; font-size:25px; letter-spacing:-.02em; }
  body.login-screen .login-action { width:100%; min-height:48px; display:flex; align-items:center;
                                    justify-content:center; gap:10px; border-radius:10px;
                                    font-weight:700; box-shadow:0 5px 13px rgba(14,159,110,.2); }
  .login-action svg { width:16px; height:16px; transition:transform .16s ease; }
  .login-action:hover svg { transform:translateX(3px); }
  @media (max-width:760px) {
    body.login-screen main { padding:24px 18px; }
    body.login-screen #page { padding:34px 26px; border-radius:17px; }
    body.login-screen #page h2 { font-size:23px; }
  }
  @media (max-width:480px) {
    body.login-screen header { padding:9px 16px; }
    body.login-screen header h1 { font-size:14px; }
    body.login-screen #page { padding:30px 22px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .login-action svg { transition:none; }
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none">
      <path d="M5 6.75A3.75 3.75 0 0 1 8.75 3h7.5A3.75 3.75 0 0 1 20 6.75v4.5A3.75 3.75 0 0 1 16.25 15H12l-4.5 3v-3.17A3.75 3.75 0 0 1 5 11.25v-4.5Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>
      <path d="M9 8.8h.01M15 8.8h.01M9 11.5h6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    </svg></span>
    <span><h1>威信AI智能助理</h1></span>
  </div>
  <span class="badge" id="ver"></span>
  <span class="badge" id="acct">未登录</span>
  <span class="spacer"></span>
  <button id="acctBtn" data-action="login">登录产品账号</button>
  <span class="badge" id="update" hidden></span>
  <span class="badge" id="conn" data-kind="pending" role="status" aria-live="polite" hidden></span>
</header>

<nav id="stepper" aria-label="办理流程"></nav>

<main>
  <div id="banner"></div>
  <div class="card" id="page"><p class="empty">正在加载…</p></div>
</main>

<footer id="footer" hidden><div class="inner" id="actions"></div></footer>

<script>
const DATADIR = "__DATADIR__";
// 读取过程的一句小字（服务端在提取数据库密钥时给），显示在「刷新列表」下面，
// 不占顶部横幅。
let READING_NOTICE = "";
// 数据与隐私说明：由服务端从 merchant_text.PRIVACY_TEXT 注入（只有一份）。
const PRIVACY = __PRIVACY__;
const $ = (id) => document.getElementById(id);
let LAST = null;          // 最近一次 /api/state
let PICKED = new Set();   // 会话勾选（按名字）。轮询重画时不能丢。
let PICKED_SEEDED = false;// 服务端已存的范围只播种一次，之后以界面为准
let CONV_CACHE = "";      // 会话列表内容的指纹：没变就不重画，勾选框不闪
let CONV_TAB = "group";  // 联系人和群组分开展示，默认优先显示群组
let CONV_TAB_MANUAL = false;
let CONV_REFRESHING = false;
let CONV_REFRESH_STATUS = "";
let CONV_REFRESH_STATUS_KIND = "muted";
let CHECK_SENT = false;   // 微信状态只自动检测一次，不跟着轮询反复戳
let LAST_STEP = "";       // 上一步是哪一屏：进屏类动作只在"刚进来"时触发
let ROLE = "order";       // 整理角色：选中项（页面只在提交时用它，不参与流程判断）
let LAST_ORDERS = [];     // 最近一次拉到的订单（点行看全文时用）
// 退出登录后要一直摆着的那句话。轮询每秒重画一次横幅，只在点击时 note() 一下
// 会被下一次 render() 用 state.notice 覆盖掉 —— 商户看到的是一闪而过，
// 等于没说"数据还在"。重新登录后清掉。
let LOGGED_OUT_NOTE = "";

const esc = (s) => String(s == null ? "" : s)
  .replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// 只在内容真的变了才写 DOM：1 秒一次的轮询不能让滚动跳、勾选掉。
const cache = {};
function setHtml(id, html) {
  if (cache[id] === html) return;
  cache[id] = html;
  $(id).innerHTML = html;
}

// 这一轮分析是什么时候开始的。模型出第一个字之前可能等几十秒，
// 没有它就报不出"已经等了 N 秒"。
let ANALYSIS_STARTED_AT = 0;

// 服务正常时不占用顶栏；只有断连时显示状态，成功轮询后自动收起。
// 短时断连先自己重试（重启服务那几秒就长这样，不值得吓人）；一直连不上
// 就直说该怎么办 —— 只喊「正在重试」，商户能做的那件事（关掉窗口重开）
// 就永远没人告诉他，他只会觉得软件死了。
const CONN_GIVE_UP_MS = 15000;
let connFailedSince = 0;
function setConnectionStatus(connected) {
  const status = $("conn");
  status.hidden = !!connected;
  if (connected) {
    connFailedSince = 0;
    return;
  }
  if (!connFailedSince) connFailedSince = Date.now();
  const giveUp = Date.now() - connFailedSince > CONN_GIVE_UP_MS;
  status.dataset.kind = "error";
  status.textContent = giveUp ? "连不上本机服务，请关掉窗口重开" : "连接中断，正在重试…";
  // 徽标只有几个字，久连不上时在正文顶上再说一遍：数据都在本机，重开不用重读。
  if (giveUp) {
    note("连不上本机服务。请关掉这个窗口再打开；本机数据都在，不用重新读取。", "bad");
  }
}

async function post(path, body) {
  try {
    const r = await fetch(path, {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(body || {})});
    setConnectionStatus(true);
    const data = await r.json();
    if (!r.ok) { note(data.error || ("请求失败：" + r.status), "bad"); return data; }
    return data;
  } catch (e) {
    setConnectionStatus(false);
    return null;
  }
}

async function get(path) {
  try {
    const r = await fetch(path);
    setConnectionStatus(true);
    return await r.json();
  } catch (e) {
    setConnectionStatus(false);
    return null;
  }
}

function note(text, kind) {
  setHtml("banner", text ? '<div class="msg ' + (kind || "warn") + '">' + esc(text) + "</div>" : "");
}

// ---------------------------------------------------------------- 步骤条

function renderStepper(data) {
  const rows = data.steps || [];
  const current = data.step;
  let html = "";
  rows.forEach((row, i) => {
    // 状态优先用服务端给的（它就表示"走到哪一步"）；current 只兜底。
    const state = row.state || (row.id === current ? "current" : "todo");
    if (i) html += '<span class="arrow" aria-hidden="true"></span>';
    html += '<span class="step ' + state + '"' +
              (state === "current" ? ' aria-current="step"' : "") + '>' +
              '<span class="dot">' + (state === "done" ? "✓" : row.number) + "</span>" +
              '<span class="name">' + esc(row.short) + "</span>" +
            "</span>";
  });
  setHtml("stepper", html);
}

// ---------------------------------------------------------------- 各步的屏

function coverageTable(rows) {
  if (!rows || !rows.length) return '<p class="empty">还没有读取结果。</p>';
  let html = "<table><tr><th>会话</th><th>本次条数</th><th>状态</th></tr>";
  for (const row of rows) {
    let status = "正常";
    if (row.skipped) status = "已跳过";
    if (row.error) status = row.error;
    if (row.truncated) status += "（读满了）";
    if (row.unresolved) status += "（" + row.unresolved + " 条发送人没认出来）";
    if (!row.error && !row.skipped && !row.count) {
      status += (row.fetched === 0)
        ? "（读取器拿回 0 条，这不等于群里没消息）"
        : "（读取器拿回 " + row.fetched + " 条，但本次一条都没进库）";
    }
    html += "<tr><td>" + esc(row.label || row.name) + "</td><td>" + row.count +
            "</td><td>" + esc(status) + "</td></tr>";
  }
  return html + "</table>";
}

const PAGES = {
  login(data) {
    const acct = data.account || {};
    // 未登录首屏**只放功能性内容**：一个动作（登录），没有说明性长句。
    // 「登录后会发生什么」「为什么必须登录」这类话商户只会读成"这段不用管"，
    // 反而拖慢那一下点击 —— 这一条已经在本文件里被删过两次，别再写回来。
    const body = (acct.logged_in
      ? '<p>已经登录：<b>' + esc(acct.name || acct.merchant_id || "（未显示名称）") + "</b></p>" +
        quotaHtml(acct)
      : '<button class="primary big login-action" data-action="login">立即登录' +
          '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M4 10h11m-4-4 4 4-4 4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
        "</button>");
    // 「下一步」灰不灰只认服务端给的门禁（``state.product_gate_open``）：
    // 页面自己按 ``logged_in`` 再判一次就是两套判据 —— 一处放行、一处拦下。
    const actions = acct.logged_in
      ? [{label:"下一步", action:"advance",
          primary:data.product_gate_open, disabled: !data.product_gate_open}]
      : [];
    return {body: body, actions: actions};
  },

  wechat(data) {
    const w = data.wechat || {};
    const rows = [];
    if (w.button) rows.push({label:w.button, action:"start-wechat"});
    rows.push({label:"重新检测", action:"check"});
    // 读不到界面时的救援。只在"检测过、且确实读不到"时出现 ——
    // 一进来还没检测就摆出来，只会让人以为软件觉得微信坏了。
    if (w.checked && w.state === "unreadable") {
      rows.push({label:"重启微信", action:"restart-wechat"});
    }
    rows.push({label:"下一步", action:"advance", primary:true, disabled:!w.ready});
    return {
      body: '<p><b>' + esc(w.title || "正在检测微信状态…") + "</b></p>" +
            '<p>' + esc(w.detail || "") + "</p>",
      actions: rows,
    };
  },

  conversations(data) {
    const rows = data.conversations || [];
    return {
      body: '<div class="conv-header"><div class="row conv-toolbar">' +
              '<button class="refresh-button" data-action="refresh-conversations" ' +
              'aria-busy="false">刷新列表</button>' +
              '<span id="convRefreshStatus" class="conv-refresh-status" role="status" ' +
              'aria-live="polite"></span></div></div>' +
            '<div class="conv-sticky">' + conversationTabsHtml(rows) +
            conversationSelectionActionsHtml(rows) + "</div>" +
            '<div id="convlist">' + conversationsHtml(rows) + "</div>" +
            '<p class="hint">只列微信当前能看到的最近会话，不等于全部历史。</p>' +
            // 明示披露：密钥怎么来的、数据去哪里 —— 折叠块放在读取动作发生的
            // 这一屏，点开才展开，不挡操作（登录屏保持"只放功能内容"的规矩）。
            '<details class="disclosure"><summary>数据与权限说明</summary>' +
            '<p>点「提取密钥」时，本工具会读取微信进程内存，目的是获取本机微信数据库' +
            '的解密密钥；密钥只保存在你电脑上（Windows 加密存储），不做其他用途。' +
            '聊天记录从你电脑上的微信本地数据库解密读取，解密结果只留在本机、用完即删。' +
            '使用「智能整理」时，你所选会话的消息文本（模型支持时含图片）会发送到 JYI 的 ' +
            'AI 网关用于生成分析；除此之外，本工具不上传你的任何数据。</p></details>' +
            // 范围确认与选择会话同一屏：两个读取按钮的差别要说在这里。
            '<p class="hint">「更新消息」= 上次之后的新消息；「读取全部」= 所选会话全部重读。' +
            "读取时要在本机解密微信的数据库，比较占机器，读完前请稍等（这期间别关微信）。</p>",
      actions: [{label:"更新消息", action:"choose-read", mode:"update"},
                {label:"读取全部", action:"choose-read", mode:"all", primary:true}]
        // 走完过的商户改主意要能直接回到第一步重来；
        // 第一次用的人还在第一轮里，用「上一步」就够，不摆这个按钮。
        .concat(data.can_go_home ? [{label:"重新开始", action:"home"}] : []),
    };
  },

  read(data) {
    const p = data.progress;
    const isReading = Boolean(p && !p.finished);
    let body = "";
    if (p) {
      const pct = p.total ? Math.round(p.done * 100 / p.total) : 0;
      body += '<div class="bar"><i style="width:' + pct + '%"></i></div>' +
              '<p class="hint">已读 ' + p.done + " / " + p.total + "（" + pct + "%）" +
              (p.current ? "　当前：" + esc(p.current) : "") +
              (p.message ? "<br>" + esc(p.message) : "") + "</p>";
    } else if (data.busy) {
      body += '<p class="muted">正在读取……</p>';
    }
    const h = data.history;
    if (h) {
      // 核对与读取同屏：读到的条数/状态/缺口就画在这里，看完直接点下一步。
      body += (h.headline ? "<p><b>" + esc(h.headline) + "</b></p>" : "") +
              coverageTable(h.coverage);
    }
    let actions;
    if (isReading) {
      actions = [{label:"停止读取", action:"cancel-read"}];
    } else if (h) {
      // 读取完成后只保留流程导航；需要换范围或重读时，先回到会话选择。
      actions = [{label:"重新选择会话", action:"back"},
                 {label:"下一步", action:"advance", primary:true}];
    } else {
      actions = [{label:"更新消息", action:"read", mode:"update"},
                 {label:"读取全部", action:"read", mode:"all", primary:true}];
    }
    return {
      body: body || '<p class="muted">还没有开始读。</p>',
      actions,
    };
  },

  analyze(data) {
    const a = data.analysis;
    const roles = data.roles || [];
    const selectedRole = roles.find((role) => role.id === ROLE) || {};
    // 点了「开始 AI 整理」就换一屏：这一屏只讲"正在分析"，选择、额度、说明
    // 都留在上一态。以前是把流式正文插到角色卡前面靠排序顶到首屏，
    // 窗口一矮或角色一多，正文又会被挤下去，商户就以为模型没在输出。
    if (data.busy) {
      // 只有报告类角色会把正文逐段流出来；订单角色拿的是结构化结果，
      // 摆一个永远「等待首段内容」的空框就成了假动作。
      const streams = selectedRole.output_kind === "report";
      // 左边是模型正在吐的字，右边是这次要盯的类别（标题来自服务端给的
      // sections，页面不写死类别名）。分栏借的是控制台那套两栏卡片
      // （console.py 的 .grid2）—— 一屏里同时看"写到哪"和"哪些要管"。
      //
      // 报告类角色才有类别可分；订单角色产出的是订单表，没有"报告章节"。
      const sections = currentSections(data);
      const left = '<section class="analysis-side">' +
        '<div class="analysis-live-head"><span class="analysis-live-dot" ' +
          'aria-hidden="true"></span><span>' +
          (streams ? "AI 正在分析 · 实时输出" : "AI 正在分析") + "</span>" +
        (selectedRole.name
          ? '<span class="analysis-live-role">' + esc(selectedRole.name) + "</span>" : "") +
        "</div>" +
        '<p class="analysis-wait" id="analysisProgress"></p>' +
        // 推理过程：正文之前模型要"想"，实测这段最长 48 秒。它**不是**结论，
        // 所以要单独一块、折叠起来、标清楚；出正文之后自动收起。
        '<details class="analysis-reasoning" id="analysisReasoningBox" hidden>' +
          "<summary>推理过程（模型内部思考，不是分析结论）</summary>" +
          '<pre id="analysisReasoning"></pre></details>' +
        (streams
          ? '<pre id="analysisLiveText" class="analysis-live-text waiting">' +
            "等待 AI 首段内容…</pre>"
          : '<p class="analysis-live-progress"><span class="spinner" ' +
            'aria-hidden="true"></span>正在逐批送模型分析，结果整理好会自动显示。</p>') +
        "</section>";
      const right = sections.length
        ? '<section class="analysis-side"><h4>需要关注的类别</h4>' +
          '<div class="analysis-sections" id="analysisSections"></div></section>'
        : "";
      return {
        body: '<div class="analysis-split">' + left + right + "</div>" +
              '<p class="hint">跑完会自动进下一步，这一屏放着不用管。</p>',
        // 分析跑着的时候不给「上一步」：退到别的屏，后台线程跑完还会
        // 把人拽回来（以前那一下是硬跳）。
        actions: [{label:"正在分析，请稍候", disabled:true}],
      };
    }
    // 跑完了先停在原地给商户看结论：直播正文就是这次的结果（多批时是各批
    // 连起来），看完自己点「下一步」看完整报告 —— 以前跑完自动换屏，
    // 流式输出一闪而过，商户根本没看到"AI 刚才写了什么"。
    if (data.analysis_ready) {
      const when = a && (a.reused_from_store || a.restored) ? "上次" : "本次";
      return {
        body: '<div class="msg good">分析完成：' + when + '「' +
              esc((a && a.role_name) || selectedRole.name || "整理") +
              '」已由 AI 处理 ' + ((a && a.processed) || 0) + ' 条消息。</div>' +
              '<pre id="analysisLiveText" class="analysis-live-text"></pre>' +
              '<p class="hint">上面就是这次的分析输出。点「下一步」看整理好的完整报告，' +
              "或再整理一遍（按用量计费）。</p>",
        actions: [{label:"下一步，查看完整报告", action:"advance", primary:true},
                  {label:"再整理一遍", action:"analyze"}],
      };
    }
    let body = quotaHtml(data.account) + "";
    if (roles.length) {
      body += '<section class="role-section" aria-label="整理角色"><div class="role-heading">' +
        '<h3>选择整理角色</h3><p>所有角色都会调用 AI，报告结构随角色变化</p></div>' +
        '<div class="role-grid" id="roles" role="radiogroup" aria-label="选择整理角色">' +
        roles.map((role) =>
          '<label class="pick-role"><span class="role-top"><span class="role-name">' +
          esc(role.name) + '</span><input type="radio" name="role" value="' +
          esc(role.id) + '"' + (role.id === ROLE ? " checked" : "") +
          ' aria-label="' + esc(role.name) + '"></span>' +
          (role.description ? '<span class="role-description">' +
            esc(role.description) + "</span>" : "") +
          '<span class="role-mode ' + (role.uses_model ? "uses-ai" : "no-ai") + '">' +
          (role.uses_model ? "调用 AI" : "本机规则整理（不调用模型）") +
          "</span></label>").join("") + "</div></section>";
    }
    // 上次点了「读取全部」、全量还没做：先把范围和账单说在前头 —— 全量是
    // 商户的明确选择（只要增量的人会点「更新消息」），但不能让它无声发生。
    if (data.full_analysis_pending) {
      body += '<div class="msg warn">上次点了「读取全部」：下一次整理会把所选会话的' +
              '全部消息（不止新增）交给 AI，按用量计费。只要新增的话，' +
              '回上一步点「更新消息」再整理。</div>';
    }
    body += '<p class="hint">点「开始整理」会把所选消息的文本（模型支持时含图片）发送给 AI 云网关；' +
            "每个角色各自记进度：同一个角色再跑只分析上次之后的新消息；" +
            "换角色会把同一批消息重新分析一遍（按用量计费）。一次最多 200 条，" +
            "第一次取最近的一批，之后按进度往后追。</p>";
    if (a) {
      if (a.output_kind === "report") {
        body += '<p class="hint">' + (a.restored ? "上次「" : "本次「") +
                esc(a.role_name || "角色整理") + "」已由 AI 处理 " +
                (a.processed || 0) + " 条消息。</p>";
      } else {
        body += '<table><tr><th>处理</th><th>生成订单</th><th>需要核对</th></tr>' +
                "<tr><td>" + a.processed + "</td><td>" + a.with_result +
                "</td><td>" + a.needs_review + "</td></tr></table>";
      }
      if (a.reused_from_store) {
        body += '<p class="hint">这次没有新消息，下面显示的是上次生成的报告。</p>';
      }
      if (a.already_analyzed) {
        body += '<p class="hint">已整理过、本次没重复提交 ' + a.already_analyzed + " 条。</p>";
      }
    }
    return {
      body: body,
      actions: [{label:selectedRole.action_label || "开始 AI 整理", action:"analyze", primary:true},
                {label:"上一步", action:"back"}],
    };
  },

  results(data) {
    const a = data.analysis || {};
    const role = (data.roles || []).find((item) => item.id === a.role_id) || {};
    const sections = a.sections || role.sections || [];
    if ((a.output_kind || role.output_kind) === "report") {
      const sectionChips = sections.map((section) =>
        '<span class="role-section-chip">' + esc(section) + "</span>").join("");
      return {
        body: '<section class="role-result-hero"><span class="role-result-kicker">' +
              (a.restored ? "上次的 AI 整理报告" : "AI 角色整理完成") + '</span>' +
              '<h3>' + esc(a.role_name || role.name || "本次整理") + ' · 整理报告</h3>' +
              '<p>' + esc(role.description || "已根据所选角色整理本次读取的消息。") + '</p>' +
              (sectionChips ? '<div class="role-section-chips" aria-label="报告主题">' +
                sectionChips + "</div>" : "") + '</section>' +
              '<div class="result-stat"><strong>' + (a.processed || 0) +
              '</strong><span>条消息已由 AI 分析</span></div>' +
              (a.partial_error ? '<div class="msg warn">' + esc(a.partial_error) + "</div>" : "") +
              (a.reused_from_store
                ? '<div class="msg warn">这次没有新消息，显示的是上次生成的报告。</div>' : "") +
              '<div id="roleReport"></div>' + endCards(),
        // 报告和导出是这一屏的主动作，跟着底栏走 —— 正文往下滚也点得到。
        // 「重新开始」回第一步：跑完一轮，要么在这屏看产物，要么从头再来。
        actions: [{label:"查看 AI 整理报告", action:"role-report", primary:true},
                  {label:"导出完整包（含 AI 结果）", action:"export"},
                  {label:"重新开始", action:"home"}],
      };
    }
    return {
      body: '<p class="hint">' +
            (a.uses_model ? "这一轮由 AI 分析产生，用量按当前账号规则结算。"
                          : "这一轮未调用 AI。") +
            "</p>" +
            '<p>处理 ' + (a.processed || 0) + " 条消息，生成订单 " +
            (a.with_result || 0) + " 单，需要核对 " + (a.needs_review || 0) + " 单" +
            (a.role_name ? "（角色：" + esc(a.role_name) + "）" : "") + "。</p>" +
            '<div id="roleReport"></div><div id="orders"></div>' + endCards(),
      actions: [{label:"查看订单", action:"orders"},
                {label:"导出完整包（含 AI 结果）", action:"export"},
                {label:"重新开始", action:"home"}],
    };
  },
};

// 额度：进度条 + 百分比（和登录屏同一套说法，来自 data.account）。
// 没有**真**额度数据时一个字都不写 —— 默认那份 QuotaView 讲的是
// 「未登录产品账号」，摆在「已登录」旁边就是自相矛盾。
function quotaHtml(acct) {
  acct = acct || {};
  if (!acct.quota_headline) return "";
  const details = [];
  if (acct.plan) details.push("套餐：" + esc(acct.plan));
  if (acct.period_end) details.push("有效期至 " + esc(String(acct.period_end).slice(0, 10)));
  let progress = "";
  if (!acct.unlimited) {
    const used = Math.max(0, Math.min(100, Number(acct.used_percent) || 0));
    const left = 100 - used;
    // 条画**剩余**：满额时是满的。以前画已用，于是"额度剩余 100%"配一条空条，
    // 商户以为额度没了（报的就是这个）。数字两个都给，免得又要猜。
    progress = '<div class="bar" role="progressbar" aria-label="剩余额度" aria-valuemin="0" ' +
               'aria-valuemax="100" aria-valuenow="' + left + '"><i style="width:' + left + '%"></i></div>';
    details.unshift("剩余 " + left + "%（已用 " + used + "%）");
  }
  return '<section class="quota-panel" aria-label="账号额度"><div class="quota-head">' +
         '<p class="quota-headline">' + esc(acct.quota_headline) + '</p>' +
         '<button class="quota-action" data-action="recharge">充值 / 续期</button></div>' +
         progress + (details.length ? '<p class="quota-detail">' + details.join("　") + "</p>" : "") +
         "</section>";
}

// 合规陈述放在首页最底部、默认折叠：该说的都在，但不挡人。
// 只放在首页 —— 登录页保持"只有一个动作"。
function privacyCard() {
  if (!PRIVACY) return "";
  return '<details class="card privacy-card" style="margin-top:12px;background:var(--bg)">' +
    '<summary class="hint">数据与隐私说明</summary>' +
    '<pre style="white-space:pre-wrap;font:inherit;margin:8px 0 0">' + esc(PRIVACY) + "</pre></details>";
}

// 名单上传、隐私说明、日志：跑完一轮顺手能碰到的地方（结果屏底部）。
// 不再单设一页"首页" —— 「重新开始」直接回第一步，没有别的落脚点。
function endCards() {
  return referenceCard() + privacyCard() +
    '<p class="hint" style="margin-top:12px"><button class="file" data-action="logs">运行日志</button></p>';
}

function referenceCard() {
  // 参考信息：商品/客户名单决定"认不认得出来"，没有入口就只能靠命令行导入。
  return '<details class="card" style="margin-top:12px;background:var(--bg)">' +
    "<summary>商品与客户名单（上传一次，以后自动认人认货）</summary>" +
    '<p class="hint">先下载模板，按模板填写后上传（.xlsx）。表里那行「示例」不用删，' +
    "导入时会跳过；重复上传不会产生重复条目。</p>" +
    '<div class="row" style="margin-top:8px">' +
      '<a class="file" href="/api/reference/template?kind=products" download>下载商品模板</a>' +
      '<label class="file">上传填写好的商品表<input type="file" accept=".xlsx,.csv" ' +
        'data-kind="products" hidden></label>' +
      '<a class="file" href="/api/reference/template?kind=customers" download>下载客户模板</a>' +
      '<label class="file">上传填写好的客户表<input type="file" accept=".xlsx,.csv" ' +
        'data-kind="customers" hidden></label>' +
    "</div><div id=\"referenceResult\"></div></details>";
}

// 分会话报告：一次选了多个会话时，每段前面本来就有「【会话名】」，
// 但整段拼在一起读起来像一份报告。这里给每个会话一个可折叠的块 ——
// 商户想只看某个群时不用在长文里翻。
// 数据来自 report.groups（落盘那份和内存那份都用这个键，见 web_server）。
function roleGroupBlocks(report) {
  const groups = report.groups || [];
  if (!groups.length) return "";
  const blocks = groups.map((item, index) =>
    '<details class="card" style="margin-top:10px;background:var(--bg)"' +
      (index === 0 ? " open" : "") + ">" +
      "<summary>" + esc(item.group_label || item.group_id || ("会话 " + (index + 1))) +
      (item.message_count ? "（" + item.message_count + " 条）" : "") + "</summary>" +
      '<div class="ai-report-content">' + reportTextHtml(item.report || "") + "</div>" +
    "</details>").join("");
  return '<p class="hint">本次分析了 ' + groups.length +
         " 个会话，下面按会话分开列（点会话名展开）。合并全文在最下面。</p>" + blocks;
}

function roleReportHtml(payload) {
  const report = payload && payload.report;
  if (!report) {
    return '<p class="empty">' + esc((payload && payload.reason) || "还没有整理明细。") + "</p>";
  }
  if (report.summary) {
    const sections = (report.sections || []).map((section) =>
      '<span class="role-section-chip">' + esc(section) + "</span>").join("");
    const groups = report.groups || [];
    const head = '<div class="ai-report-head"><strong>AI 整理报告</strong>' +
      '<span>' + esc(report.role_name || report.role_id) + " · " + (report.total || 0) +
      ' 条消息</span></div>' +
      (sections ? '<div class="role-section-chips" aria-label="报告主题">' + sections + "</div>" : "") +
      (report.partial_error ? '<div class="msg warn">' + esc(report.partial_error) + "</div>" : "");
    // 只有一份（单会话）时就是一段正文，不摆折叠块 —— 一个会话套一层折叠是多余的。
    if (groups.length <= 1) {
      return '<section class="ai-report">' + head +
        '<div class="ai-report-content">' + reportTextHtml(report.summary) + "</div></section>";
    }
    return '<section class="ai-report">' + head + roleGroupBlocks(report) +
      '<details class="card" style="margin-top:10px;background:var(--bg)">' +
      "<summary>合并全文</summary>" +
      '<div class="ai-report-content">' + reportTextHtml(report.summary) + "</div>" +
      "</details></section>";
  }
  const buckets = report.buckets || {};
  let html = '<p class="hint">「' + esc(report.role_name || report.role_id) +
             "」共 " + (report.total || 0) + " 条。</p>";
  for (const [label, items] of Object.entries(buckets)) {
    html += "<h3>" + esc(label) + "（" + (items || []).length + " 条）</h3>";
    html += (items || []).length
      ? "<table><tr><th>发送人</th><th>内容</th><th>时间</th></tr>" +
        items.slice(0, 50).map((item) =>
          "<tr><td>" + esc(item.sender) + "</td><td>" + esc(cellText(item.content)) +
          "</td><td>" + esc(item.sent_at) + "</td></tr>").join("") + "</table>"
      : '<p class="empty">这一类没有内容。</p>';
  }
  return html;
}

function reportTextHtml(text) {
  const lines = String(text || "").split(/\r?\n/);
  const html = [];
  let inList = false;
  const closeList = () => {
    if (inList) { html.push("</ul>"); inList = false; }
  };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { closeList(); continue; }
    const heading = line.match(/^#{1,3}\s+(.+)$/);
    if (heading) {
      closeList();
      html.push("<h4>" + esc(heading[1]) + "</h4>");
      continue;
    }
    const bullet = line.match(/^(?:[-*•]|\d+[.、])\s*(.+)$/);
    if (bullet) {
      if (!inList) { html.push("<ul>"); inList = true; }
      html.push("<li>" + esc(bullet[1]).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>") + "</li>");
      continue;
    }
    closeList();
    html.push("<p>" + esc(line).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>") + "</p>");
  }
  closeList();
  return html.join("");
}

// ---------------------------------------------------------------- 分析中那一屏

// 这一轮选的是哪个角色、它有哪些类别。类别名来自服务端（roles.result_sections），
// 页面不写死 —— 换个角色，右边的类别跟着换。
function currentRole(data) {
  const roles = (data && data.roles) || [];
  return roles.find((role) => role.id === ROLE) || {};
}

function currentSections(data) {
  const role = currentRole(data);
  return role.output_kind === "report" ? (role.sections || []) : [];
}

// 流式正文 → 类别。报告的标题是「### 群内提问」这种（见 roles.result_sections），
// 按标题切段；还没写到的那一节先空着，等模型写过来。
function splitByHeadings(text) {
  const out = {};
  const lines = String(text || "").split(/\r?\n/);
  let current = "";
  for (const raw of lines) {
    const heading = raw.match(/^\s{0,3}#{1,6}\s*(.+?)\s*$/);
    if (heading) {
      current = heading[1].replace(/[【】\[\]]/g, "").trim();
      if (!(current in out)) out[current] = "";
      continue;
    }
    if (current) out[current] += (out[current] ? "\n" : "") + raw;
  }
  return out;
}

// 标题对不上就退一步按包含关系找 —— 模型偶尔会写成「群内提问（待回复）」。
function sectionBody(bodies, name) {
  if (bodies[name]) return bodies[name];
  for (const key of Object.keys(bodies)) {
    if (!key) continue;
    if (key.indexOf(name) >= 0 || name.indexOf(key) >= 0) return bodies[key];
  }
  return "";
}

function analysisSectionsHtml(sections, text) {
  if (!sections || !sections.length) return "";
  const bodies = splitByHeadings(text);
  return sections.map((name) => {
    const body = String(sectionBody(bodies, name) || "").trim();
    return '<section class="analysis-section' + (body ? " has-content" : "") + '">' +
      "<h5><span>" + esc(name) + "</span>" +
      '<span class="muted">' + (body ? "有内容" : "等模型输出") + "</span></h5>" +
      (body ? "<pre>" + esc(body) + "</pre>"
            : '<p class="empty" style="padding:2px 0">还没有内容。</p>') +
      "</section>";
  }).join("");
}

// 表格里的字要能看出被截断（和 merchant_text.cell 一个规矩：40 字 + …）。
function cellText(text, limit) {
  const value = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
  const max = limit || 40;
  return value.length <= max ? value : value.slice(0, max) + "…";
}

// 这份会话名单是怎么来的？必须如实说。
// wcdb = 解密数据库，真名真人数。商户看到残缺名单时要能知道点刷新重试。
function sourceStatus(data) {
  const source = data.conversation_source || "";
  const cache = data.conversation_cache_info || "";
  if (source === "wcdb") {
    return "来源：解密数据库（真名、真人数）。" + (cache ? " " + cache + "。" : "");
  }
  return cache ? "还没有本次的会话来源信息。" + cache + "。" : "点击「刷新列表」读取微信会话。";
}

function conversationTabCounts(rows) {
  return {
    group: rows.filter((row) => row.kind === "group").length,
    friend: rows.filter((row) => row.kind === "friend").length,
    other: rows.filter((row) => row.kind !== "group" && row.kind !== "friend").length,
  };
}

function conversationTabsHtml(rows) {
  const counts = conversationTabCounts(rows);
  const tabs = [
    {id:"group", label:"群聊", count:counts.group},
    {id:"friend", label:"联系人", count:counts.friend},
  ];
  if (counts.other) tabs.push({id:"other", label:"未分类", count:counts.other});
  return '<div class="tabs" role="tablist" aria-label="会话类型">' +
    tabs.map((tab) => '<button type="button" class="tab' +
      (CONV_TAB === tab.id ? " active" : "") + '" role="tab" data-conv-tab="' +
      tab.id + '" aria-selected="' + (CONV_TAB === tab.id) + '">' + tab.label +
      '<span class="tab-count">' + tab.count + "</span></button>").join("") +
    "</div>";
}

function conversationSelectionActionsHtml(rows) {
  const visibleRows = conversationRowsForTab(rows);
  const label = CONV_TAB === "group" ? "群聊" : CONV_TAB === "friend" ? "联系人" : "未分类";
  const disabled = visibleRows.length ? "" : " disabled";
  return '<div class="conv-selection-actions"><span class="conv-scope">当前分类：' +
    label + "（" + visibleRows.length + "）　已选 <b id=\"pickcount\" aria-live=\"polite\">0</b> 个会话</span>" +
    '<button id="convBulkAction" type="button" data-action="toggle-current-conversations"' + disabled +
    ' aria-pressed="false">全选当前分类</button></div>';
}

// 服务端存着上次的读取范围：进这一屏时要显示成「已勾选」。
// 不播种的话，界面写「已选 0 个会话」，而直接点读取仍然按旧范围读 ——
// 界面和事实对不上，商户会以为自己的选择丢了。
function seedPicked(data) {
  // 服务端给的是会话**键**（name），和勾选框的 value 是同一个东西。
  const wanted = new Set(data.selected || []);
  for (const row of data.conversations || []) {
    if (wanted.has(row.name)) PICKED.add(row.name);
  }
}

function conversationRowsForTab(rows) {
  if (CONV_TAB === "group") return rows.filter((row) => row.kind === "group");
  if (CONV_TAB === "friend") return rows.filter((row) => row.kind === "friend");
  return rows.filter((row) => row.kind !== "group" && row.kind !== "friend");
}

function updateConversationBulkAction(rows) {
  const button = $("convBulkAction");
  if (!button) return;
  const visibleRows = conversationRowsForTab(rows || []);
  const allSelected = visibleRows.length > 0 && visibleRows.every((row) => PICKED.has(row.name));
  button.disabled = visibleRows.length === 0;
  button.textContent = allSelected ? "取消全选" : "全选当前分类";
  button.setAttribute("aria-pressed", String(allSelected));
}

function updateConversationRefreshUi() {
  const button = document.querySelector('button[data-action="refresh-conversations"]');
  if (button) {
    button.disabled = CONV_REFRESHING;
    button.setAttribute("aria-busy", String(CONV_REFRESHING));
    button.innerHTML = CONV_REFRESHING
      ? '<span class="spinner" aria-hidden="true"></span>正在刷新…'
      : "刷新列表";
  }
  const status = $("convRefreshStatus");
  if (status) {
    // 提取数据库密钥那几十秒，这里显示一句灰字；它比"正在刷新…"更说明问题。
    status.dataset.kind = READING_NOTICE ? "muted" : CONV_REFRESH_STATUS_KIND;
    status.textContent = READING_NOTICE || CONV_REFRESH_STATUS;
  }
  const list = $("convlist");
  if (list) list.setAttribute("aria-busy", String(CONV_REFRESHING));
}

function conversationKindLabel(kind) {
  if (kind === "group") return "群聊";
  if (kind === "friend") return "联系人";
  return "其他";
}

function conversationsHtml(rows) {
  if (!rows || !rows.length) return '<p class="empty">还没有会话，点「刷新列表」。</p>';
  const visibleRows = conversationRowsForTab(rows);
  if (!visibleRows.length) return '<p class="empty">当前分类没有会话。</p>';
  let html = "<table><tr><th></th><th>会话</th><th>类型</th><th>成员</th></tr>";
  for (const row of visibleRows) {
    const checked = PICKED.has(row.name) ? " checked" : "";
    // 显示用 label（微信没给名字时是「未命名群聊」），勾选的 value 仍用 name
    // （数据键：消息/订单的 group_id 就是它）。原始 ID 只放进 title，鼠标悬停才看。
    const label = row.label || row.name;
    const title = row.name && row.name !== label ? ' title="' + esc(row.name) + '"' : "";
    html += '<tr><td><input type="checkbox" class="pick" value="' + esc(row.name) + '"' +
            checked + "></td><td" + title + ">" + esc(label) + "</td><td>" +
            esc(conversationKindLabel(row.kind)) +
            "</td><td>" + row.members + "</td></tr>";
  }
  return html + "</table>";
}

// ---------------------------------------------------------------- 渲染

function render(data) {
  LAST = data;
  setConnectionStatus(true);
  // 版本号：客服排障第一句就问它，界面上必须有（来源只有 version.py 一处）。
  $("ver").textContent = data.version ? "v" + data.version : "";
  const acct = data.account || {};
  // 重新登录之后，"数据还在"那句话就该消失（它是退出时的说明，不是常驻提示）。
  if (acct.logged_in) LOGGED_OUT_NOTE = "";
  document.body.classList.toggle("login-screen", data.step === "login" && !acct.logged_in);
  $("acct").textContent = acct.logged_in
    ? ("已登录：" + (acct.name || acct.merchant_id || "（未显示名称）"))
    : "未登录";
  // 登录页的正文已经呈现账号状态；顶栏只在其他步骤保留全局账号状态。
  $("acct").hidden = data.step === "login";
  const btn = $("acctBtn");
  btn.textContent = acct.logged_in ? "退出登录" : "登录产品账号";
  btn.dataset.action = acct.logged_in ? "logout" : "login";
  // 未登录时登录入口在居中首屏卡片；顶部不重复显示账号状态或登录入口。
  btn.hidden = !acct.logged_in && data.step === "login";

  // 新版暂存好了就说一句：静默更新不能等于一声不吭。
  // 变量别叫 note —— 那会把上面的 note() 函数遮住，render 直接抛异常。
  const noteEl = $("update");
  noteEl.hidden = !data.update_note;
  noteEl.textContent = data.update_note || "";

  renderStepper(data);
  // 分析这一轮什么时候开始的（用来报"已经等了 N 秒"）。分析结束就归零，
  // 下一轮重新计时。
  if (data.busy && !ANALYSIS_STARTED_AT) ANALYSIS_STARTED_AT = Date.now();
  if (!data.busy && ANALYSIS_STARTED_AT) ANALYSIS_STARTED_AT = 0;
  // 登录失败可能属于一次旧的 OAuth 尝试；如果当前账号已有效登录，
  // 不要继续把那次回调超时显示成当前故障。
  const staleLoginError = acct.logged_in && String(data.error || "").startsWith("登录失败：");
  if (data.error && !staleLoginError) note(data.error, "bad");
  else note(data.notice || LOGGED_OUT_NOTE, "good");
  READING_NOTICE = String(data.reading_notice || "");

  // 微信那一屏：监视器还没检测过就先画"正在检测"，并且只触发一次检测。
  const w = data.wechat || {};
  if (data.step === "wechat" && !w.checked && !CHECK_SENT) {
    CHECK_SENT = true;
    post("/api/wechat/check");
  }

  // 进「智能整理」那一屏时后台刷一次真额度：登录时存下的那份先显示着，
  // 刷到就覆盖（顶栏和这一屏一起变）。只在**刚进这一屏**时触发 ——
  // 每秒一次的轮询不该变成每秒一次的网络请求。
  if (data.step === "analyze" && LAST_STEP !== data.step && acct.logged_in) {
    post("/api/account/quota");
  }
  // 新步骤从顶部开始看。尤其分析页会显示实时正文，不能继承读取长表格后
  // 的滚动位置，否则实时输出区虽在首屏，窗口却仍停在旧滚动位置。
  if (LAST_STEP && LAST_STEP !== data.step) {
    const main = document.querySelector("main");
    if (main) main.scrollTop = 0;
  }
  LAST_STEP = data.step;

  // 进「选择会话」时把服务端已存的范围播种成勾选状态（只播一次，
  // 之后以界面上的勾选为准 —— 轮询不能把商户刚改的勾选冲掉）。
  if (data.step === "conversations" && !PICKED_SEEDED) {
    PICKED_SEEDED = true;
    seedPicked(data);
    CONV_CACHE = "";
  }
  if (data.step === "conversations" && !CONV_TAB_MANUAL) {
    const rows = data.conversations || [];
    const counts = conversationTabCounts(rows);
    CONV_TAB = counts.group ? "group" : counts.friend ? "friend" : counts.other ? "other" : "group";
  } else if (data.step === "conversations" && CONV_TAB === "other") {
    const counts = conversationTabCounts(data.conversations || []);
    if (!counts.other) {
      CONV_TAB = counts.group ? "group" : counts.friend ? "friend" : "group";
      CONV_TAB_MANUAL = false;
    }
  }
  if (data.step === "conversations" && !CONV_REFRESH_STATUS && !CONV_REFRESHING) {
    CONV_REFRESH_STATUS = sourceStatus(data);
    CONV_REFRESH_STATUS_KIND = "muted";
  }

  // 步骤表里没有的（理论上只剩未进向导的空值）退回登录前置页 —— 那是
  // 每次启动的落点，也是最安全的兜底。
  const page = PAGES[data.step] || PAGES.login;
  const view = page(data);
  // 屏标题也用服务端给的步骤标题 —— 页面里不写第二份（写了两份一定漂）。
  const row = (data.steps || []).find((item) => item.id === data.step);
  const heading = row ? row.title : (data.step_title || "可以开始用了");
  // 登录屏不挂那种小标签：它和下面的标题（「登录产品账号」）是同一个意思，
  // 属于只做说明、不带状态也不带动作的文案。
  const html = "<h2>" + esc(heading) + "</h2>" + view.body;
  if (cache["page"] !== html) {
    cache["page"] = html;
    $("page").innerHTML = html;
  }
  const liveOutput = $("analysisLiveText");
  const fragment = String(data.summary_stream || "");
  const reasoning = String(data.reasoning_stream || "");
  if (liveOutput) {
    const nearBottom = liveOutput.scrollHeight - liveOutput.scrollTop - liveOutput.clientHeight < 36;
    liveOutput.textContent = fragment || "等待 AI 首段内容…";
    liveOutput.classList.toggle("waiting", !fragment);
    if (nearBottom) liveOutput.scrollTop = liveOutput.scrollHeight;
  }
  // 推理过程：正文还没出来时它是唯一在动的东西，所以默认展开、跟着滚；
  // 正文一开始就自动收起（想看再点开）—— 思考过程不是报告，别占着屏幕。
  const reasoningBox = $("analysisReasoningBox");
  const reasoningText = $("analysisReasoning");
  if (reasoningText) {
    reasoningText.textContent = reasoning;
    if (reasoningBox) {
      reasoningBox.hidden = !reasoning;
      if (reasoning) {
        const pre = reasoningText;
        const nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 36;
        if (nearBottom) pre.scrollTop = pre.scrollHeight;
        if (!fragment && !reasoningBox.dataset.auto) reasoningBox.open = true;
        if (fragment && reasoningBox.open && !reasoningBox.dataset.auto) {
          reasoningBox.dataset.auto = "1";
          reasoningBox.open = false;
        }
      }
    }
  }
  // 分析进度：模型出第一个字之前可能等几十秒（实测最长 48 秒），
  // 这一段时间不给一句"正在分析第几个会话"，商户只会以为卡死了。
  const progress = $("analysisProgress");
  if (progress) {
    const line = String(data.analysis_progress || "");
    if (line) {
      progress.textContent = line;
    } else if (fragment) {
      progress.textContent = "正在写报告…";
    } else if (reasoning) {
      progress.textContent = "模型正在思考（已写出 " + reasoning.length + " 字）…";
    } else {
      const seconds = ANALYSIS_STARTED_AT
        ? Math.max(0, Math.round((Date.now() - ANALYSIS_STARTED_AT) / 1000)) : 0;
      progress.textContent = "模型还在读这一批（已等 " + seconds + " 秒）…";
    }
  }
  // 右栏的类别：跟着流式正文一起长出来。
  const sectionsBox = $("analysisSections");
  if (sectionsBox) {
    const sectionsHtml = analysisSectionsHtml(currentSections(data), fragment);
    if (sectionsBox.innerHTML !== sectionsHtml) sectionsBox.innerHTML = sectionsHtml;
  }

  // 会话表：指纹没变就一个字节都不动 —— 1 秒一次的轮询不能把勾选冲掉。
  if (data.step === "conversations") {
    const fingerprint = CONV_TAB + "|" + JSON.stringify(data.conversations || []);
    if (fingerprint !== CONV_CACHE) {
      CONV_CACHE = fingerprint;
      const box = $("convlist");
      if (box) box.innerHTML = conversationsHtml(data.conversations || []);
    }
    const counter = $("pickcount");
    if (counter) counter.textContent = String(PICKED.size);
    updateConversationBulkAction(data.conversations || []);
  }

  const footer = $("footer");
  footer.hidden = !view.actions.length;
  setHtml("actions", view.actions.map(actionHtml).join(""));
  updateConversationReadActions();
  updateConversationRefreshUi();
}

// 底栏按钮点开的正文（报告 / 日志 / 订单）可能根本不在视野里：渲染完滚过去，
// 否则商户看到的就是"点了没反应"。focus 只为把读屏的位置也带过去。
function revealBox(box) {
  if (!box) return;
  box.setAttribute("tabindex", "-1");
  box.scrollIntoView({block:"start", behavior:"smooth"});
  box.focus({preventScroll:true});
}

function actionHtml(item) {
  // 有些底栏位只是一句状态（比如「正在分析，请稍候」），没有动作可点。
  return '<button' + (item.action ? ' data-action="' + esc(item.action) + '"' : "") +
    (item.mode ? ' data-mode="' + esc(item.mode) + '"' : "") +
    (item.primary ? ' class="primary"' : "") +
    (item.disabled ? " disabled" : "") + ">" + esc(item.label) + "</button>";
}

function updateConversationReadActions() {
  const hasSelection = PICKED.size > 0;
  document.querySelectorAll('#actions button[data-action="choose-read"]').forEach((button) => {
    button.disabled = !hasSelection;
    button.setAttribute("aria-disabled", String(!hasSelection));
    button.title = hasSelection ? "" : "请先选择至少一个会话";
  });
}

// ---------------------------------------------------------------- 动作

document.addEventListener("click", async (event) => {
  // 订单行：点一下看被表格截掉的全文。
  const tr = event.target.closest("tr[data-order]");
  if (tr && !event.target.closest("button")) {
    const row = LAST_ORDERS[Number(tr.dataset.order)];
    const box = $("orderDetail");
    if (row && box) {
      box.innerHTML = '<div class="card" style="margin-top:10px;background:var(--bg)">' +
        "<p><b>" + esc(row.customer || "（未绑定客户）") + "</b>　" + esc(row.status) +
        "　" + esc(row.total == null ? "" : row.total) +
        (row.group ? "　来自：" + esc(row.group) : "") + "</p>" +
        '<pre class="full">' + esc(row.summary) + "</pre></div>";
    }
    return;
  }
  const tab = event.target.closest("button[data-conv-tab]");
  if (tab) {
    const nextTab = tab.dataset.convTab;
    if (nextTab && nextTab !== CONV_TAB) {
      CONV_TAB = nextTab;
      CONV_TAB_MANUAL = true;
      CONV_CACHE = "";
      const main = $("main");
      const scrollTop = main ? main.scrollTop : 0;
      if (LAST) render(LAST);
      if (main) main.scrollTop = scrollTop;
    }
    return;
  }
  const btn = event.target.closest("button[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === "login") { await post("/api/login"); return; }
  if (action === "logout") {
    await post("/api/logout");
    // 商户最误会的一件事：退出登录 = 数据没了。其实消息和订单都还在这台电脑上，
    // 主动说出来，免得他去重读一遍（白费工夫，还可能重复下单）。
    LOGGED_OUT_NOTE = "已退出登录。本机数据都还在，不需要重新读取。";
    note(LOGGED_OUT_NOTE, "good");
    return;
  }
  if (action === "check") { await post("/api/wechat/check"); return; }
  if (action === "restart-wechat") {
    if (!confirm("会先关闭微信再重新打开。微信里没发出去的内容可能会丢失，\n"
                 + "聊天记录在本地，不会丢。\n\n确定重启吗？")) return;
    note("正在重启微信，请稍等…", "warn");
    await post("/api/wechat/restart");
    return;
  }
  if (action === "start-wechat") {
    await post("/api/wechat/start");
    // 让监视器盯着：商户在微信窗口里扫码，登录成功后状态自己会变。
    await post("/api/wait-for-wechat");
    return;
  }
  if (action === "refresh-conversations") {
    if (CONV_REFRESHING) return;
    const previousRows = (LAST && LAST.conversations) || [];
    const previousFingerprint = JSON.stringify(previousRows);
    CONV_REFRESHING = true;
    CONV_REFRESH_STATUS = "正在读取微信当前可见的会话…";
    CONV_REFRESH_STATUS_KIND = "muted";
    updateConversationRefreshUi();
    try {
      const data = await get("/api/conversations?force=1");
      if (!data || data.error || !Array.isArray(data.conversations)) {
        CONV_REFRESH_STATUS = (data && data.error)
          ? "刷新失败：" + data.error
          : "刷新失败，请检查微信状态后重试。";
        CONV_REFRESH_STATUS_KIND = "error";
        return;
      }
      const rows = data.conversations;
      const validNames = new Set(rows.map((row) => row.name));
      for (const name of PICKED) {
        if (!validNames.has(name)) PICKED.delete(name);
      }
      const changed = JSON.stringify(rows) !== previousFingerprint;
      const counts = conversationTabCounts(rows);
      const cacheInfo = data.cache_info || "";
      if (LAST) {
        LAST.conversations = rows;
        LAST.conversation_cache_info = cacheInfo;
      }
      CONV_CACHE = "";
      CONV_REFRESH_STATUS = rows.length
        ? (changed ? "已更新" : "列表已是最新") + " · 共 " + rows.length +
          " 个会话（群聊 " + counts.group + "，联系人 " + counts.friend + "）" +
          (cacheInfo ? " · " + cacheInfo : "")
        : "刷新完成 · 当前没有可用会话。";
      CONV_REFRESH_STATUS_KIND = "success";
      CONV_REFRESHING = false;
      if (LAST && LAST.step === "conversations") render(LAST);
    } catch (error) {
      CONV_REFRESH_STATUS = "刷新失败：" + (error.message || "请稍后重试。");
      CONV_REFRESH_STATUS_KIND = "error";
      note(CONV_REFRESH_STATUS, "bad");
    } finally {
      CONV_REFRESHING = false;
      updateConversationRefreshUi();
    }
    return;
  }
  if (action === "toggle-current-conversations") {
    const rows = conversationRowsForTab((LAST && LAST.conversations) || []);
    const allSelected = rows.length > 0 && rows.every((row) => PICKED.has(row.name));
    for (const row of rows) allSelected ? PICKED.delete(row.name) : PICKED.add(row.name);
    CONV_CACHE = "";
    if (LAST) render(LAST);
    return;
  }
  if (action === "back") {
    const out = await post("/api/wizard/back");
    if (out && !out.ok) note(out.reason, "warn");
    return;
  }
  if (action === "advance") {
    const out = await post("/api/wizard/advance");
    if (out && !out.ok) note(out.reason, "warn");
    return;
  }
  if (action === "choose-read") {
    // 选择范围与开始读取同一屏：**先存勾选再读**，否则读的还是上次存的范围。
    // 空勾选由服务端拒绝（判据在向导那边），页面不自己再判一次。
    if (PICKED.size === 0) {
      note("请先选择至少一个会话，再开始读取。", "warn");
      return;
    }
    await post("/api/conversations/choose", {names: [...PICKED]});
    const out = await post("/api/history/read", {mode: btn.dataset.mode || ""});
    if (out && out.ok === false) note(out.reason || "没能开始读取。", "warn");
    return;
  }
  if (action === "read") {
    await post("/api/history/read", {mode: btn.dataset.mode || ""});
    return;
  }
  if (action === "cancel-read") { await post("/api/history/cancel"); return; }
  if (action === "analyze") { await post("/api/analyze", {role: ROLE}); return; }
  if (action === "role-report") {
    // 按当前选中的角色取；服务端内存里没有就回**落盘的那份**（重启后仍然拿得到）。
    const wanted = (LAST && LAST.analysis && LAST.analysis.role_id) || ROLE;
    const payload = await get("/api/analysis/report?role=" + encodeURIComponent(wanted));
    const box = $("roleReport");
    if (box) {
      box.innerHTML = roleReportHtml(payload);
      revealBox(box);
    }
    return;
  }
  if (action === "logs") {
    const data = await get("/api/logs");
    const box = $("logsBox");
    if (!box) return;
    let html = '<p class="hint">日志文件：<code>' + esc((data && data.path) || "（未知）") +
               "</code>　出问题时把这个文件发给客服。</p>";
    html += '<pre class="logs">' + esc(((data && data.lines) || []).join("\n")) + "</pre>";
    box.innerHTML = html;
    revealBox(box);
    return;
  }
  if (action === "recharge") {
    const out = await post("/api/account/recharge");
    if (!out || !out.ok) {
      note("没能自动打开浏览器，请手动打开这个地址充值：" + ((out && out.url) || ""), "warn");
    }
    return;
  }
  if (action === "export") {
    // 先问能不能导、会跳过几条 —— 点了没反应是最糟的体验。
    const info = await get("/api/export/check");
    if (!info || !info.ok) {
      note((info && info.reason) || "现在导不了。", "warn");
      return;
    }
    if (info.skipped) {
      note("有 " + info.skipped + " 条还没整理，这次只导整理过的 " + info.rows + " 条。", "good");
    }
    window.location.href = "/api/export/bundle";
    return;
  }
  if (action === "orders") {
    const data = await get("/api/orders");
    if (!data) return;
    const rows = data.orders || [];
    LAST_ORDERS = rows;
    // 说清一共多少单：超量不提示，商户会以为看到的就是全部。
    // 「来自」一列：客户是按名字认的（同一个名字在几个群里是同一个人），
    // 不写出处，跨群的重名单子就成了查不清的账。
    const html = rows.length
      ? "<p class=\"hint\">共 " + rows.length + " 单。点一行看完整内容（表格里的字会被截断）。</p>" +
        "<table class=\"clickable\"><tr><th>订单</th><th>客户</th><th>来自</th><th>状态</th>" +
        "<th>内容</th></tr>" +
        rows.map((r, i) => '<tr data-order="' + i + '"><td>' + esc(r.id) + "</td><td>" +
          esc(r.customer) + "</td><td>" + esc(r.group || "（未记来源）") + "</td><td>" +
          esc(r.status) + "</td><td>" +
          esc(cellText(r.summary, 60)) + "</td></tr>").join("") +
        "</table><div id=\"orderDetail\"></div>"
      : '<p class="empty">还没有订单。</p>';
    const box = $("orders");
    if (box) {
      box.innerHTML = html;
      revealBox(box);
    }
    return;
  }
  if (action === "home") {
    // 分析详情 → 首页：到这一步才记「向导走完」（服务端 finish()）。
    await post("/api/wizard/home");
    await tick();
    return;
  }
});

// 勾选/角色/上传：事件委托，重画也不丢。
document.addEventListener("change", async (event) => {
  const fileInput = event.target.closest('input[type="file"][data-kind]');
  if (fileInput) {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    const kind = fileInput.dataset.kind;
    try {
        // xlsx 是二进制：按 base64 交给服务端（那边有零依赖的解析器）。
      const buffer = await file.arrayBuffer();
      let binary = "";
      const bytes = new Uint8Array(buffer);
      for (let i = 0; i < bytes.length; i += 0x8000) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      }
      const out = await post("/api/reference", {
        kind: kind, filename: file.name, data: btoa(binary)});
      const box = $("referenceResult");
      if (box) box.innerHTML = referenceResultHtml(out);
    } catch (e) {
      note("这个文件读不出来：" + e.message, "bad");
    }
    fileInput.value = "";  // 同一个文件再传一次也要能触发
    return;
  }
  const role = event.target.closest('input[name="role"]');
  if (role) { ROLE = role.value; return; }
  const box = event.target.closest("input.pick");
  if (!box) return;
  if (box.checked) PICKED.add(box.value); else PICKED.delete(box.value);
  const counter = $("pickcount");
  if (counter) counter.textContent = String(PICKED.size);
  updateConversationBulkAction((LAST && LAST.conversations) || []);
  updateConversationReadActions();
});

function referenceResultHtml(out) {
  if (!out) return "";
  if (out.error) return '<div class="msg bad">' + esc(out.error) + "</div>";
  let html = '<div class="msg good">新增 ' + (out.created || 0) + " 条，更新 " +
             (out.updated || 0) + " 条";
  if (out.skipped) html += "，跳过 " + out.skipped + " 条";
  html += "</div>";
  if (out.problems && out.problems.length) {
    // 行号是**表格里的行号**（表头算第 1 行），商户对着 Excel 就能找到那一行改。
    html += '<p class="hint">有 ' + out.problems.length + ' 行没导进来，按表格行号改好后重传：</p>' +
      '<ul class="list">' + out.problems.slice(0, 20).map((p) =>
        "<li>" + esc(p) + "</li>").join("") + "</ul>";
  }
  return html;
}

async function tick() {
  const data = await get("/api/state");
  if (!data) return null;
  try {
    render(data);
  } catch (err) {
    // 渲染出错不能把页面冻在"正在加载…"上 —— 商户只会觉得软件坏了。
    // 把错误显示出来（并留在控制台给开发看），比一声不吭强。
    console.error(err);
    cache["page"] = "";
    $("page").innerHTML = '<div class="msg bad">这一屏没能画出来：' +
      esc(err && err.message ? err.message : err) + "</div>";
  }
  return data;
}

// 轮询间隔自适应：读取/分析在跑（busy）时加密到 200ms —— 流式正文要跟手，
// 1 秒一跳在屏幕上就是"一段一段蹦"；闲时 1 秒一次就够，别白烧机器。
async function pollLoop() {
  const data = await tick();
  setTimeout(pollLoop, data && data.busy ? 200 : 1000);
}
pollLoop();
</script>
</body>
</html>
"""
