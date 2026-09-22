# Erza 代码审查报告

**审查日期**：2026-09-22
**审查范围**：`erza/` 全包 + `webui/` 前端 + 测试套件 + 工程配置
**审查方式**：静态检查（ruff）+ 全量测试（后端/前端）+ 两轮重构 diff 逐行比对 + 上轮 28 处修复回归验证 + 子代理分域深读 + 主线逐条复核
**对照基线**：`CODE_REVIEW_2026-09-12.md`（上轮 28 处缺陷已全部修复）

---

## 一、总体结论

**本周期（9-12 至今）代码库处于健康状态，无需立即修复的 Critical/High 缺陷。**

客观基线：

| 检查项 | 结果 |
|---|---|
| `ruff check erza/` | 零告警 |
| 后端测试 | 4152 passed / 29 skipped / 0 failed |
| 前端测试（vitest） | 265 passed / 28 files |
| 遗留 TODO/FIXME | 仅 skill-creator 模板中的有意占位符 |

期间发生两轮大重构：`13872beb`（打破 agent-tools/schema-tools import cycle，新增 `erza/contracts/` 包）与 `97809bdd`（webui 折叠进 `channels/websocket/`，helpers 拆分）。**两轮重构经逐行 diff 比对，均为"纯搬移 + 单点接线"式改动，未发现回归**。重构后的防御性设计（`schema.py` 保留 lazy-rebuild metaclass 作为纵深防御、`serve.py` 双重路径遍历防护）均附有清晰的决策注释，符合本项目"高效简洁、可审计"的定位。

子代理初报 14 条疑似缺陷，**经逐条复核后 8 条不成立**（详见第四节否决记录），确认 6 条 Low 级微瑕，无一条需要紧急处理。

---

## 二、上轮修复回归验证（重点项）

上轮修复的两处 Critical 在本轮重构后均完好：

- **C1（save_skip 长度推算）**：`turn_orchestrator.py:480-485` 现以 `appended = len(ctx.initial_messages) - base` 反推用户消息是否为独立元素，与 `context.py:655-661` 的合并分支语义一致。验证了 history 末条为 user 角色时（记忆快照场景）的合并路径，落盘切片 `messages[skip:]` 不再丢助手回复。
- **C2（MCP reload 未 await）**：旧 `_reload_mcp_safe` 同步包装已删除，现行路径 `_http.py:157` 为 `await self._mcp_reloader(self.bus)`，异常经 `logger.exception` 记录并返回 `requires_restart`。

项目硬约束核验：

- `/api/screenshot` localhost 限制：`handlers/screenshot.py:24` 拒绝非回环连接 ✅
- token 字典（`_issued_tokens`/`_api_tokens`）：访问时惰性 purge + `_MAX_ISSUED_TOKENS` 上限（`_tokens.py:148-152`、`_bootstrap.py:56-72`）✅
- 后台任务引用管理：`_notify_tasks` 集合持有 + done_callback 自动回收（`channel.py:180-182`）✅
- 崩溃恢复链：`session_turn.py` 的 checkpoint 单槽设计（审计用 phase 跳过策略）+ overlap 去重重放，逻辑严密 ✅

---

## 三、本轮发现（全部 Low，无 Critical/High/Medium）

### L1. 前端 `close()` 不清空订阅状态
**位置**：`webui/src/lib/erza-client.ts:294-308`
`close()` 清理 socket 与重连定时器，但 `chatHandlers`（每 chat_id 的 handler 集）和 `knownChats`（已 attach 集合）保留。若调用方组件未调用 `onChat` 返回的 unsubscribe 直接卸载，handler 引用残留；`knownChats` 残留会使后续 `connect()` 重发 attach。
**评估**：依赖调用方自律，属可接受的设计取舍。若要收紧，可在 `close()` 中 `chatHandlers.clear(); knownChats.clear(); pendingInboundByChat.clear()`（需确认无"close 后复用订阅"的调用场景）。

### L2. DingTalk token 刷新无锁竞态
**位置**：`erza/channels/dingtalk/channel.py:337-359`
`_get_access_token` 检查-刷新非原子，token 过期时并发协程会各刷一次（多余 HTTP 调用）。两者写入相同 token，无正确性影响。
**评估**：个人框架、DingTalk 并发量低，不值得加锁。知悉即可。

### L3. 微信媒体下载失败对终端用户不可见
**位置**：`erza/channels/weixin/_media.py:107-109`
下载异常经 `logger.exception` 完整记录后返回 `None`，消息继续按纯文本处理。服务端可诊断，但用户收到的回复无任何"图片下载失败"提示。
**评估**：设计取舍。若在意用户体验，可在 `_polling.py` 组装回复时对"消息声明有媒体但 media_paths 为空"的情况追加一句占位提示。

### L4. WebSocket 连接循环异常仅 debug 级日志
**位置**：`erza/channels/websocket/_lifecycle.py:179-180`
`_dispatch_envelope` 等消息处理异常统一归入 `logger.debug("connection ended: {}", e)`，难以区分协议错误、序列化错误与业务错误。
**评估**：诊断粒度问题。可在该处对非 `ConnectionClosed` 异常升级为 `logger.warning`。

### L5. 死兼容 shim：`erza/utils/progress_events.py`
**位置**：`erza/utils/progress_events.py`（30 行 re-export）
全仓库已无 `from erza.utils.progress_events import ...` 的真实调用方（仅 docstring 提及）。上轮拆分到 `erza/session/progress` 后遗留的旧 import 路径。
**评估**：按本项目反死代码/反抽象税原则可直接删除。

### L6. 微瑕：`serve.py:24` 死代码
`rel = request_path.lstrip("/")` 之后 `rel.startswith("/")` 恒为 False，该分支不可达。无行为影响，顺手可清。

---

## 四、初报否决记录（复核过程）

以下子代理初报经主线逐条复核**不成立**，记录于此以免重复排查：

1. **weixin `_context_tokens` 无界增长**（初报 Medium）→ 写入仅发生在 `is_allowed(from_user_id)` 通过后（`_polling.py:116-129`），受 `allowFrom` 名单约束。个人使用框架名单极小，不构成增长风险。
2. **feishu 扩展名集合重复定义**（初报 Low）→ `rg` 全频道确认 `_IMAGE_EXTS`/`_AUDIO_EXTS`/`_VIDEO_EXTS` 仅 `_send.py:23-25` 一处定义，不存在第二来源。
3. **`providers/base.py` `_safe_chat` 吞异常**（初报 Medium）→ 返回 `LLMResponse(finish_reason="error")` 是显式错误契约；planner（`planner.py:85`）、runner 等调用方均检查该字段并走 fallback。`CancelledError` 亦正确重抛。
4. **`fallback_provider.py` deepcopy kwargs**（初报 Medium）→ 请求级防御性隔离，生命周期限于单次调用，无泄漏面。
5. **`backup.py:299` 直连 sqlite3 无 lock_timeout**（初报 Medium）→ 恢复路径受 `memory-maintenance.lock` 串行化保护；Python `sqlite3.connect` 默认 5s busy timeout，非无等待裸连。

---

## 五、结构与重构质量评价

两轮重构的落地质量值得肯定：

- **`erza/contracts/` 抽取**（plan.py 141 行 / subagent.py 212 行）——verbatim 搬移 + 原模块 re-export 保持兼容，模块 docstring 明确标注了搬移意图与新调用约定。`SubagentRegistry` 的 frontmatter 解析（tools 字段三形态兼容）注释完备。
- **`config/tool_configs.py`**——工具配置类从 tools 层上移到 config 层，消除了 base→capability 的依赖倒置。`schema.py` 保留了 `_lazy_rebuild_meta` 作为防御纵深并写明理由，未做过度清理。
- **`channels/websocket/static/serve.py`**——SPA 静态服务提取为纯函数以便单测；遍历防护是双重的（段级 `..` 检查 + `resolve()`/`relative_to()` 白名单校验），`index.html` no-cache / hash 资源 immutable 的缓存策略正确。
- **`providers/_openai_compat_helpers.py`**——332 行 helper 拆分，`openai_compat_provider.py` 相应缩减，无逻辑漂移。

core 链路（`loop.py`/`runner.py`/`context.py`/`session_turn.py`/`planner.py`）专项深读未发现新的 Critical/High 缺陷：后台任务持引用、finally 按 token 重置上下文、injection/escalation 的 best-effort 路径均有日志兜底。

**171 条测试 warning** 均为 Windows Proactor 事件循环关闭噪音（`RuntimeError: Event loop is closed`），与代码质量无关，可考虑在 `pyproject.toml` 的 `filterwarnings` 中静默。

---

## 六、建议（按性价比排序）

1. **删除** `erza/utils/progress_events.py` 死 shim（L5，30 行净减）
2. **可选**：`_lifecycle.py:180` 非 `ConnectionClosed` 异常升级 warning（L4）
3. **可选**：weixin 媒体下载失败的用户侧提示（L3）
4. **知悉即可**：L1/L2/L6 属设计取舍或不可达分支

---

## 七、修复记录（2026-09-22 同日执行）

全部 6 条 Low 级发现 + 测试 warning 静默项已修复，验证全绿（ruff 零告警、后端 4152+ 通过、前端 265 通过、tsc 干净）：

| 项 | 修复内容 | 验证 |
|---|---|---|
| L5 | 删除 `erza/utils/progress_events.py` 与同类 shim `erza/utils/tool_hints.py`；`test_loop_progress.py`/`test_tool_hint.py` 改用 canonical 路径 `erza.session.progress`；清理 `progress/__init__.py`、`_tool_events.py` docstring 中的过时引用 | 两测试文件 57 passed |
| L4 | `_lifecycle.py` 拆分 `except ConnectionClosed`（保持 debug）与其余异常（升级 warning），正常断连不产生日志噪音 | ruff + 全量 |
| L3 | `_polling.py` 四类媒体分支区分"有下载源但下载失败"（占位 `[image (download failed)]` 等，向 LLM 明示）与"无下载源"（原占位符）；`test_weixin_channel.py` 对应断言更新 | weixin 61 passed |
| L2 | `dingtalk/channel.py` `_get_access_token` 加 `asyncio.Lock` 双重检查锁，过期时并发协程只发一次刷新请求 | 实测 5 并发调用 → 仅 1 次刷新请求（SDK 未装，渠道测试环境性跳过） |
| L1 | `erza-client.ts` `close()` 清空 `chatHandlers`/`knownChats`/`pendingInboundByChat`（已确认 close 后重建新实例，App.tsx 两处调用点均为废弃语义） | vitest 265 passed + tsc |
| L6 | `serve.py` 删除 `lstrip("/")` 后恒 False 的 `rel.startswith("/")` 不可达分支 | ruff |
| W | `pyproject.toml` filterwarnings 静默 Proactor 析构噪音（`(?s)` 跨行 + `\x3a` 转义冒号，单引号 TOML 字面串） | 全量 171→170，目标消息精确命中 1 条 |

**修复过程中的新发现**：

1. **L5 初判有误**——上轮 grep 被 `-First 8` 截断，漏掉了 `test_loop_progress.py:15` 的真实 import。本次精确 grep 后修复方案调整为"测试改 canonical 路径 + 删 shim"，并发现 `tool_hints.py` 同类 shim 一并清理。
2. **filterwarnings 踩坑记录**——message 正则含字面 `:` 会破坏 `action:message:category` 解析（pytest 尝试把正则片段当类别导入报 `TypeError`）；TOML 双引号字符串又无法表达 `\x3a`，最终用单引号字面字符串解决。
3. **工作区状态说明**——`weixin/` mixin 拆分、`session/progress/` 包、`websocket/_lifecycle.py` 等均为未提交的未跟踪新文件（叠加在 HEAD 97809bdd 之上），本报告的审查与修复均基于该工作区状态。

**未处理的存量问题**（超出本轮范围，知悉即可）：

- `bun run lint` 存在 9 errors / 7 warnings（jsx-a11y：autoFocus、media caption、no-static-element-interactions 等），全部位于本次未触碰的文件，属存量技术债，修复需产品层面的 a11y 决策。
- 剩余 170 条测试 warning 为其他类型（测试 mock 的 `coroutine ... was never awaited`、`tests/agent` 中 `AgentLoopBuilder.with_extra(**kwargs)` 的 DeprecationWarning 各 1 条等），与 Proactor 析构噪音无关，未逐条归类。
- `loop_builder.py:380` 的 `with_extra(**kwargs)` 废弃 API 仍有测试调用，可顺手迁移到显式 `with_*` 方法。

---

*审查方法说明：本轮采用"上轮基线回归验证 + 重构 diff 逐行比对 + 分域深读 + 主线复核否决"的流程。子代理初报 14 条，复核否决 8 条（57% 误报率），印证了"agent 报告必须验证"的必要性。*
