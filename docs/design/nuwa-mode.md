# 女娲模式（Nüwa Mode）技术方案

> 一句话：用一段自然语言描述，生成一个可编辑的智能体草稿，并从市场挑工具、从技能市场挑规范、从指定站点抓资料入知识库，全程草稿态、可回退、可审批。
>
> 状态：**已按 C 方案（在线优先）实现，本地完成，未 push、未部署**。实现清单与实测结果见第 14 节。
>
> 前提：女娲模式**允许平台自身访问互联网**（市场检索与资料抓取），但这与"智能体工具出网"是两个独立开关，见第 5 节。

---

## 1. 范围

### 做

1. 自然语言 → 智能体骨架（名称、slug、定位、系统提示词、模型、说话方式档、可见范围、欢迎语），逐步可编辑。
2. **在线优先**检索 **MCP 工具来源** 与 **技能** 目录，失败自动回落本地缓存；按描述打分排序，用户勾选后挂载（落库前走审批）。
3. 按描述**联网**检索资料（用户给定 URL + 同域浅层发现），预览抓取条目，确认后写入指定或新建知识库。
4. 产物一律 `AgentProfile(status=draft)`，沿用既有 A7 发布检查与 A8 版本归档。
5. 向导状态服务端持久化，刷新/中断可恢复。

### 不做（首版明确排除）

- 不做通用网页爬虫，不做全站抓取，不接入第三方搜索 API（需要外部 key，另行决策）。
- 不做自动发布：女娲只产出草稿。
- 不做"自动信任"：外部 MCP 来源与技能未经管理员审批不进运行时。
- 不做多轮自由对话式澄清（首版是一次结构化生成 + 可编辑 + 重生成，见第 7 节）。

---

## 2. 现状盘点（可复用，不重复造）

| 能力 | 现有实现 | 女娲怎么用 |
|---|---|---|
| 智能体定义与状态机 | `backend/apps/agenthub/models.py` `AgentProfile`（draft/published/…，`snapshot()`、`publish()`） | 产物直接建 `AgentProfile`，不新建"女娲智能体"表 |
| 挂载关系 | `AgentMCPServer`（per-agent 工具勾选）、`AgentSkill` | 勾选结果写这两张中间表 |
| 工具来源 | `backend/apps/mcp/models.py` `MCPServer` / `MCPTool`；`services.py` 的命令前缀白名单、`egress_gate()`、`resolved_private_hit()` | 外部来源落库必须过这些闸门 |
| 技能审批 | `backend/apps/skills/models.py`（`pending→approve`、`body_hash`、`SkillVersion`）、`scanner.py` 规则集、`services.sync_mirror()` | 外部技能直接进 `pending`，复用 C3 审核与镜像 |
| 单级审批范式 | `AgentImportRequest`（提交人不可自审、产物为草稿） | 外部 MCP / 技能落库申请照此实现，独立表 |
| 知识库 | `KnowledgeBase` / `Document` / `DocumentChunk`、`services.extract_text()`、`process_document()`、`search_chunks()` | 抓取正文写成 `Document` 后直接跑既有流水线 |
| 模型调用 | `backend/apps/common/llm.py` `chat_completion()`，配置唯一来源 `SystemSetting(llm_*)` | 骨架生成只走这里，未配置时明确报错不降级 |
| 文本向量 | `backend/apps/common/embedding.py` `embed_texts()`（fastembed bge-small-zh，已装） | 市场匹配打分复用，不引入新依赖 |
| 审计 | `users.models.log_audit` | 会话、同步、导入、抓取全部入审计 |

**部署事实**：`deploy/systemd/` 只有 `lasso.service`，没有 Celery worker 单元；`process_document()` 在请求内同步执行。因此女娲所有步骤**同步实现**，用"逐条结果 + 上限 + 超时"控制时长，不引入异步队列。

---

## 3. 端到端流程与状态机

```
① 描述  →  ② 骨架  →  ③ 工具/技能  →  ④ 资料  →  ⑤ 确认建草稿
   │          │            │              │            │
 可重生成   可逐字段改   可勾选/跳过    可勾选/跳过   可只建草稿
```

`NuwaSession.status`：
`drafting`（在①–②）→ `matching`（③）→ `knowledge`（④）→ `ready`（⑤待确认）→ `created`（已建草稿）/ `abandoned`。
每步保存即推进，`step` 记录当前步，前端按 `session.step` 恢复。会话 7 天过期（`expires_at`），过期后只读，可"另存为新会话"。

审批分支：③ 选中的外部 MCP / 技能若未落库，则生成 `NuwaImportRequest(pending)`，**不阻塞**建档——草稿可以只挂"已在本地的"候选，未审批项在确认页列为待办。

---

## 4. 市场从哪来（已实测，非推测）

### 4.1 MCP 工具市场：官方 MCP Registry

- 端点：`https://registry.modelcontextprotocol.io/v0/servers`，实测可用。
- 支持 `?search=<关键词>&limit=<n>`，返回 `metadata.nextCursor` 分页；条目结构：
  - `remotes[]`：`{type: "streamable-http", url}` → 映射 `MCPServer.transport=http_mcp, url`
  - `packages[]`：`{registryType: "npm"|"pypi", identifier, runtimeHint: "npx"|"uvx", transport.type: "stdio", runtimeArguments[], environmentVariables[{name,isSecret,isRequired,default}]}` → 映射 `transport=stdio, command, args, secrets.env`
  - `repository`、`websiteUrl`、`version`、`description`、`title`
- **注意**：同一接口同时存在 `2025-07-09 / 2025-09-29 / 2025-12-11` 三个 schema 版本，解析必须按"字段是否存在"降级，原始 JSON 全量留档。
- 补充源（可选、非首版必需）：`https://github.com/modelcontextprotocol/servers`（官方参考实现列表），仅作目录补充。

### 4.2 技能市场：`anthropics/skills`

- 实测：`api.github.com/repos/anthropics/skills` 公开可读，默认分支 `main`，结构为 `skills/<name>/SKILL.md`（可再带脚本/资源子文件）。
- **许可证字段为 `null`（仓库未声明 LICENSE）** —— 这是硬约束，见 5.3 与 12.3。
- 同步方式：GitHub Contents API 列目录 + `raw.githubusercontent.com` 取 `SKILL.md` 原文；匿名 API 限流 60 次/小时，同步必须"一次列目录 + 逐条限速"，并支持带 `GITHUB_TOKEN` 的预同步。
- 首版只取**单文件 `SKILL.md`**；带脚本附件的技能包标为"首版不支持"，不落库（避免执行外部脚本）。

### 4.3 本地缓存（兜底与离线包）

- 所有在线命中条目都落 `MarketItem` 表；检索是"在线优先 + 本地兜底"，缓存是**降级路径**，不是唯一路径。
- `python manage.py nuwa_sync --source all` 用于三种场景：冷启动预热、断网/被墙环境、导出离线包。产出：
  1. 数据库 `MarketItem`；
  2. `data/nuwa/market-cache.json`（离线包，含 source/条目/抓取时间/哈希）；
  3. 内网机器 `python manage.py nuwa_sync --import data/nuwa/market-cache.json` 灌入，全程不出网。
- 每个源记录 `last_sync_at / item_count / last_sync_status`；UI 显示"缓存 · 同步于 2026-11-xx，共 N 条"，过期（>30 天）标黄。

---

## 5. 出网边界与降级

### 5.1 两把开关，语义分开

**两个开关互不影响，这一点必须在文档与界面里写死**：

| 开关 | 管什么 | 默认 | 关掉后的影响面 |
|---|---|---|---|
| `mcp_egress_enabled`（既有） | **已装配智能体在会话里调用外部工具**是否能出网 | 关 | 只影响运行时工具调用（`egress_blocked`），**不影响女娲检索与抓取** |
| `nuwa_market_online`（新增） | **女娲自身**的市场检索与资料抓取是否能出网 | **开** | 只影响女娲向导，**不影响已装配智能体的工具调用** |

- 关闭 `nuwa_market_online` 后：女娲回落到本地目录与"仅上传/粘贴"，已上线智能体的工具照常按 `mcp_egress_enabled` 执行。
- 关闭 `mcp_egress_enabled` 后：女娲仍可在线检索市场、抓资料（这是平台侧出网），但导入的 MCP 来源在运行时会被 `egress_gate()` 判为"出网未开放"，候选卡片与确认页照实标注。
- 两条链路各自写审计：`nuwa_market_fetch` / `nuwa_web_fetch` 与既有 `egress_toggle` 分开。

### 5.2 在线优先 + 本地兜底（C 方案落地）

- **主路径（默认）**：`POST /sessions/{id}/matches/` 先实时请求官方 registry / GitHub API；命中条目写入 `MarketItem`（`fetched_at` 刷新），**只入缓存不建来源**。
- **兜底路径**：主路径失败、超时、被墙、或开关被管理员关闭时，自动回落本地 `MarketItem` 查询。两条路径结果按 `external_id+version` 合并去重，在线结果优先，本地结果标 `cached`。
- **不报错**：接口始终 200，返回 `{items, mode: online|cached|mixed, degraded_reason, cache_synced_at}`；前端按 `mode` 显示来源徽章（"实时" / "缓存 · 同步于 X"）。
- **预同步仍保留**：`nuwa_sync` 与 `market-cache.json` 用于内网/断网环境与首次冷启动；在线可用的环境里它变成"预热与离线包导出"，不再是唯一来源。
- **冷启动**：本地目录为空且在线失败 → 返回空列表 + `degraded_reason='offline_no_cache'` + 文案"当前无法访问市场目录，且本地无缓存；请联系管理员执行 `nuwa_sync` 或导入离线包"。不返回假数据。

### 5.3 超时与重试（固定值，不开放给现场配置）

| 动作 | 连接超时 | 总超时 | 重试 | 熔断/中止 |
|---|---|---|---|---|
| 市场检索（registry `search`） | 5 s | 12 s | 1 次（退避 1 s） | 失败即回落缓存 |
| 技能目录（GitHub Contents API） | 5 s | 12 s | 1 次（退避 1 s） | 同上 |
| 技能正文（raw.githubusercontent） | 5 s | 10 s | 1 次 | 单条失败只跳过该条 |
| 目录同步 `nuwa_sync` | 5 s | 单源 120 s | 每页 1 次 | 连续 3 页失败即停，保留已入库部分 |
| 资料抓取（单 URL） | 10 s | 30 s | 1 次（退避 2 s） | 连续 3 条失败中止本轮 |
| 单域限速 | — | — | — | 同域 1 req/s，单次 ≤20 条、单条正文 ≤200 KB |
| LLM 骨架生成 | 10 s | 60 s | 修复重试 1 次 | 超时返回可编辑空白骨架 + 错误 |

- 重试用尽即降级，不向上抛 5xx；`degraded_reason` 取值：`switch_off` / `timeout` / `network_error` / `rate_limited` / `offline_no_cache`。
- 在线结果与缓存结果都写 `MarketItem`，命中率与降级次数进审计，便于运维判断网络状况。

### 5.4 关闭状态下怎么降级（不报错）

- 检索：只查 `MarketItem`，顶部显示"离线目录（同步于 X）"；目录为空 → 冷启动文案（见 5.2）。
- 需要出网的候选：照常展示，标 `需出网` 徽章；选中后若运行时 `egress_gate()` 判定不可连，确认页照实说明（复用既有 `user_message/admin_message` 文案）。
- 知识抓取：开关关闭时不发请求，步骤 ④ 降级为"仅上传文件 / 粘贴正文"，并说明"女娲在线抓取已关闭"。
- 实时同步按钮：开关关闭时置灰，tooltip 给管理员操作指引。

---

## 6. 安全：外部内容落库前的校验

### 6.1 MCP 来源

| 检查 | 规则 | 不通过 |
|---|---|---|
| 传输类型 | 只接受 `streamable-http(s)` 或 `npm/pypi` 包 | 丢弃并计入 `skipped` |
| 协议 | http 只允许 `https://`；解析后域名非私网（复用 `is_private_host` / `resolved_private_hit`） | 标 `blocked` |
| 命令 | `runtimeHint` 必须在既有白名单（`npx -y` / `uvx` / `python` / `node`），用 `match_command_prefix()` 复核 | 拒绝落库 |
| 参数 | 拒绝 `; | & $ ` ( ) < > \n` 与绝对路径；`runtimeArguments` 原样拼接后整体过白名单 | 拒绝落库 |
| 环境变量 | 键名 `^[A-Z][A-Z0-9_]{0,63}$`；**全部写入 `secrets_enc`**（`MCPServer` 没有明文 env 字段），`isSecret` 的额外标注 | 键名非法即拒绝 |
| 出网声明 | `egress_domains = [host]`（精确域名，不带协议路径） | 缺 host 拒绝 |
| 工具启停 | 新来源 `enabled_tools=[]`，工具默认关闭；审批后由管理员 `test()` 发现再逐个开 | 默认全关 |
| 信任度 | publisher 命中白名单（`modelcontextprotocol`、`anthropics`、`microsoft`、`github`）→ `trusted`；其余 `unverified` | `unverified` 必须人工审批 |

### 6.2 技能

- 只取 `SKILL.md` 单文件，`raw.githubusercontent.com` 直取（不解析 HTML），≤ 32 KB，正文 ≤ 8000 字（沿用 `MAX_BODY_CHARS`）。
- frontmatter 必须含 `name` / `description`，缺则记为 `invalid` 并给原因。
- 用 `skills/scanner.py` 现有规则集扫描（高危：外发凭据、`curl|bash`、`base64 -d`、写 `.ssh`/`.env` 等）；高危项走既有"风险确认（`risk_ack`）+ 管理员审核"双闸。
- 附件/脚本：首版**不接收**，只提示"该技能含脚本，需人工以文件包方式上传"。
- 落库状态：`status=pending`、`source=repo`；未审核通过不写 `approved_body`、不写镜像目录，运行时不生效。

### 6.3 许可证

- 条目许可证写入 `MarketItem.license`；为空/未知时：候选卡片显示"许可证未声明"，审批页红字提示，导入后**仅限企业内部使用**并记入审计；不自动对外分发。
- `anthropics/skills` 当前 `license=null`，按上一条处理；企业若要再分发，需法务确认（见 12.3）。

### 6.4 审批链（复用既有范式）

- 非管理员不能建 `MCPServer` / `Skill`（现有 `IsAdminUser` 写权限不变）。
- 女娲里的外部条目 → `NuwaImportRequest(pending)`：`kind=mcp_server|skill`、`draft_payload`（已归一化）、`risk_flags`、`requested_by`。
- 管理员审批通过才创建对象；**提交人不可自审**；创建出的 MCPServer 为 `status=unknown`、工具全关；Skill 为 `pending`，仍需再走一次 C3 审核（双闸：来源审批 + 内容审核）。
- 全部动作 `log_audit`：`nuwa_session_created`、`nuwa_market_sync`、`nuwa_import_submitted/approved/rejected`、`nuwa_knowledge_imported`、`market_item_blocked`。

---

## 7. 提示词生成

**方式：模板骨架 + 单次结构化生成 + 可编辑 + 可重生成**（不选多轮问答：企业用户描述通常已足够，多轮会拖长流程且难复现）。

- 模板：服务端常量 `NUWA_PROMPT_TEMPLATE_VERSION = 'v1'`，system 段固定输出契约（只输出 JSON），user 段填 brief + 平台可选模型列表 + 可见范围枚举。
- 输出 JSON（严格字段）：

```json
{
  "name": "≤30字",
  "description": "≤200字，做什么/给谁用",
  "job_tags": ["行政"],
  "icon": "预置图标名",
  "prompt": "系统提示词，含角色/边界/输出格式/拒答口径",
  "speech_tier": "low|mid|high",
  "retrieval_tier": "few|standard|many",
  "welcome_message": "≤120字",
  "visibility_suggestion": "private|department|all",
  "knowledge_queries": ["用于资料检索的关键词，≤6条"]
}
```

- 可重复：
  - `temperature=0.2`、`max_tokens` 受限、单次调用 + **最多 1 次修复重试**（把解析错误摘要回灌）。
  - 落 `NuwaSession.generation_meta`：`model`、`template_version`、`brief_hash`、`raw_output`、`retries`、`duration_ms`。
  - 同一 `brief_hash` + 同模板 + 同模型 → 命中上次结果（"恢复上次生成"），用户显式点"重新生成"才重算。
- 可编辑：② 步每个字段都是受控表单；用户改动记 `edited_fields[]`，确认页标"已人工修改"。
- 防幻觉：
  - 模型名必须来自 `/api/agents/options/` 返回的已验证列表，不在列表内则回落平台默认并提示；
  - 提示词占位符白名单 `{current_date} {user_name} {department}`，其余 `{...}` 原样保留但提示"平台不替换"；
  - 解析失败两次 → 返回**空白可编辑骨架** + 明确错误，不编造字段。
- 欢迎语：`AgentProfile` 现无此字段，新增 `welcome_message`（`CharField(200, blank)`），并在 `snapshot()` 的 `basic` 分区纳入版本差异（老版本无该键，首版发布会显示为新增，属预期）。

---

## 8. 资料检索与入库

### 8.1 检索源（首版）

1. 用户在 ④ 步给出的 URL（互联网可达即可，无需预先登记）。
2. 站内 `sitemap.xml` / 列表页的浅层发现（同域，深度 1，≤20 条）。
3. 管理员可配置站点白名单（`SystemSetting: nuwa_knowledge_allowlist`）用于**收紧**范围：为空表示"不限域名（仍限 https 且非私网）"，非空表示仅允许名单内域名及其子域。

不接第三方搜索 API（需要外部 key）；"按主题自己找站"的能力留到 P1，由管理员配置 provider。因此首版的"检索"是**给定 URL + 同域浅层**，不是"全网搜索"——这一点在界面上要说清，避免用户误期待。

### 8.2 抓取与清洗

- `urllib.request`（标准库，不新增依赖），30 s 超时、UA 标明平台、单域名 1 req/s、单次 ≤20 条、单条正文 ≤200 KB。
- 遵守 `robots.txt`（`urllib.robotparser`），命中 Disallow 直接跳过并记原因。
- 清洗：去 `script/style/nav/footer`，正文取最长文本块，HTML→纯文本/markdown；保留 `title`、站点、抓取时间、原 URL。
- 落库：写成临时 `.md` 文件 → `Document(file_type='md')` → 调 `process_document()`（分块/向量/统计全复用）。

### 8.3 去重与引用元数据

- URL 规范化：去 `#fragment`、去 `utm_*`/`gclid` 等追踪参数、统一小写 host、去尾 `/`。
- `Document` 新增字段：`source_url`（500，索引）、`source_title`、`source_site`、`source_license`、`fetched_at`、`content_sha256`。
- 唯一约束：同知识库内 `source_url` 非空唯一（`UniqueConstraint(fields=['knowledge_base','source_url'], condition=Q(source_url__gt=''))`）；内容 `sha256` 相同则跳过并提示"与已有文档重复"。
- 检索结果里回传引用（`document_id` + `source_url`），供会话侧展示来源。

### 8.4 失败与限流

- 逐条返回 `{url, status: ok|skipped|failed, reason}`，不做"全或全无"。
- 429/5xx：单条最多重试 1 次（指数退避 2 s），仍失败记 `failed`；连续 3 条失败即中止本轮并保留已入库部分。
- 单条失败不影响步骤推进；确认页汇总"成功 N / 跳过 M / 失败 K（可展开原因）"。

---

## 9. 数据模型

### 9.1 新 app：`backend/apps/nuwa/`

```
MarketSource         外部市场源（管理员维护）
  id, name, kind(mcp_registry|skill_repo|local_catalog), base_url,
  auth_env('' | 'GITHUB_TOKEN'), enabled, is_builtin,
  sync_interval_hours(默认 168), last_sync_at, last_sync_status, last_error, item_count

MarketItem           本地目录（离线可用的唯一数据源）
  id, source FK, kind(mcp|skill), external_id, version,
  title, summary, publisher, homepage, repository_url, license,
  trust_level(trusted|unverified|blocked),
  transport(http_mcp|stdio), install_payload JSONB,   # url 或 package 细节
  egress_host, requires_egress, capabilities JSONB,   # 标签/关键词，用于打分
  content_hash, raw JSONB, status(active|stale|blocked), fetched_at
  unique(source, kind, external_id, version)

MarketSyncRun        同步批次
  id, source FK, started_at, finished_at, status, added, updated, skipped, failed,
  error, cursor

NuwaSession          向导会话（可恢复）
  id, owner FK, brief TEXT, brief_hash, step(1-5),
  status(drafting|matching|knowledge|ready|created|abandoned),
  generated JSONB, generation_meta JSONB, edited_fields JSONB,
  candidates JSONB, selections JSONB, knowledge_plan JSONB, knowledge_items JSONB,
  agent FK(null), error, created_at, updated_at, expires_at

NuwaImportRequest    外部 MCP/技能落库申请（单级审批）
  id, kind(mcp_server|skill), market_item FK, requested_by,
  draft_payload JSONB, risk_flags JSONB,
  status(pending|approved|rejected), reviewed_by, reviewed_at, review_note, result JSONB
```

### 9.2 变更既有表

- `agenthub.AgentProfile`：+`welcome_message`（CharField 200，blank），纳入 `snapshot()` 的 `basic` 分区。
- `knowledge.Document`：+`source_url/source_title/source_site/source_license/fetched_at/content_sha256` + 唯一约束。
- `SystemSetting` 键：`nuwa_enabled`(**true**)、`nuwa_market_online`(**true**)、`nuwa_knowledge_online`(**true**)、`nuwa_knowledge_allowlist`(空数组)、`nuwa_daily_session_limit`(20)。

> 资料抓取单独一个 `nuwa_knowledge_online`：管理员可以在"市场可在线、资料抓取关闭"的状态下运行（例如合规上不允许把外部网页写进知识库），两个开关都只作用于女娲，与 `mcp_egress_enabled` 无联动。

复用不新建：`MCPServer/MCPTool`、`Skill/SkillVersion`、`AgentMCPServer/AgentSkill`、`KnowledgeBase`、`AgentVersion`。

### 9.3 迁移

`nuwa/0001_initial`、`agenthub/000x_welcome_message`、`knowledge/000x_document_source`。均为加字段/加表，无需回填；`welcome_message` 空值不改变既有行为（会话页空态再用，见 10.3）。

---

## 10. 接口清单

### 10.1 女娲主流程（登录即可用，前缀 `/api/nuwa/`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/sessions/` | 建会话 + 同步生成骨架（LLM 未配置 → 400 明确文案） |
| GET | `/sessions/` | 我的会话列表（分页，仅本人） |
| GET | `/sessions/{id}/` | 会话详情（按 `step` 返回已算好的段落） |
| PATCH | `/sessions/{id}/` | 编辑 `brief` / `generated` 字段 |
| POST | `/sessions/{id}/regenerate/` | 重新生成骨架（保留上一版可回退） |
| POST | `/sessions/{id}/matches/` | 市场匹配（可带 `kinds=['mcp','skill']`、`include_egress=true`）；返回 `mode/degraded_reason/cache_synced_at` |
| POST | `/sessions/{id}/selections/` | 保存勾选（本地已有项直接记 id；外部项生成导入申请） |
| POST | `/sessions/{id}/knowledge/plan/` | 由 brief 生成检索关键词（可手改） |
| POST | `/sessions/{id}/knowledge/preview/` | 在线抓取预览（不落库，逐条状态；`urls[]` 必填，可附 `discover=true` 做同域浅层） |
| POST | `/sessions/{id}/knowledge/commit/` | 确认写入（`kb_id` 或新建 `{name, scope}`） |
| POST | `/sessions/{id}/create-agent/` | 建草稿（幂等；返回 `agent_id`、待审批项） |
| POST | `/sessions/{id}/abandon/` | 放弃 |

### 10.2 市场与审批

| 方法 | 路径 | 权限 |
|---|---|---|
| GET/POST | `/api/nuwa/sources/` | 管理员 |
| GET/POST | `/api/nuwa/switches/` | 管理员（`nuwa_market_online` / `nuwa_knowledge_online` / 白名单），变更入审计 |
| POST | `/api/nuwa/sources/{id}/sync/` | 管理员（受 `nuwa_market_online` 约束） |
| GET | `/api/nuwa/items/` | 登录（本地目录浏览/搜索，分页） |
| POST | `/api/nuwa/items/{id}/block/` | 管理员 |
| GET | `/api/nuwa/import-requests/` | 管理员全量 / 普通用户仅本人 |
| POST | `/api/nuwa/import-requests/{id}/approve/` | 管理员且非提交人 |
| POST | `/api/nuwa/import-requests/{id}/reject/` | 同上 |

### 10.3 复用

- `GET /api/agents/options/`（模型、三档、授权档位、内置能力）
- `POST /api/agents/`、`PUT/PATCH /api/agents/{id}/`、`GET /api/agents/{id}/mount-options/`
- `GET|POST /api/agents/{id}/publish-check/`、`POST /api/agents/{id}/publish/`
- `GET/POST /api/knowledge/bases/`、`POST /api/knowledge/documents/upload/`
- `POST /api/mcp/servers/{id}/test/`、`POST /api/mcp/servers/{id}/tools/toggle/`

管理命令：`python manage.py nuwa_sync --source all|mcp-registry|anthropics-skills [--import <file>] [--dry-run]`。

---

## 11. 前端交互

### 11.1 入口与路由

- 路由：`/agents/nuwa`（新增，挂在既有 `Agents` 路由下）；入口：Agents 列表页右上角"女娲模式"按钮（与"新建智能体"并列）。
- 新文件：`frontend/src/pages/Agents/Nuwa/index.tsx` + `Step1Brief.tsx` / `Step2Skeleton.tsx` / `Step3Market.tsx` / `Step4Knowledge.tsx` / `Step5Confirm.tsx`，API 封装 `frontend/src/api/nuwa.ts`。

### 11.2 五步与可编辑点

| 步 | 内容 | 可编辑/可操作 | 空态与错误 |
|---|---|---|---|
| ① 描述 | 大文本框 + 3 个示例 | 描述、目标受众、可选部门 | LLM 未配置：顶部提示 + 跳"系统设置" |
| ② 骨架 | 名称/定位/提示词（折叠展开）/模型/说话方式/查询档/可见范围/欢迎语 | 全部字段；"重新生成"（保留上一版）、"查看原始输出" | 解析失败：展示空白骨架 + 错误原因 + 重试 |
| ③ 工具与技能 | 两组候选卡片（名称、来源、能力摘要、许可证、需出网、已安装、匹配分与命中理由） | 勾选；"仅看本地已有"；提交未落库项为审批 | 顶部按 `mode` 显示"实时" / "缓存 · 同步于 X"；在线失败时黄条说明降级原因，不清空已展示缓存；目录为空：指引 `nuwa_sync` |
| ④ 资料 | URL 输入（多行，可粘贴）+ 同域浅层开关 + 抓取结果列表（标题/站点/状态） | 勾选条目；选目标知识库或新建；跳过本步 | 开关关闭：降级为上传/粘贴并说明；单条失败仅标红，不中断 |
| ⑤ 确认 | 变更清单 + 风险提示（未审批工具、许可证未知、需出网、外链来源） | "创建草稿"；"回到上一步" | 幂等：重复点击返回同一 `agent_id` |

### 11.3 中断恢复

- 每步动作都写服务端；刷新后按 `session.step` 回到该步，已生成内容不再重算。
- 会话列表（`/agents/nuwa` 空态下方）显示进行中会话与"继续"。
- 浏览器关闭超过 7 天 → 会话过期只读，提示"另存为新会话"。

---

## 12. 风险与限制

1. **外部 schema 漂移**：registry 同时存在三个 schema 版本，字段可能再变。对策：解析器按字段存在性降级、`raw` 全量留档、同步失败不影响既有目录。
2. **供应链**：npm/pypi 包名可被抢注，技能正文可能含提示词注入。对策：命令白名单 + 参数校验 + 工具默认关 + 来源审批 + 正文扫描 + 内容审核双闸。
3. **许可证不明**：`anthropics/skills` 未声明 LICENSE；企业再分发有法律风险。对策：仅内部使用、界面明示、审批留痕；对外分发需法务确认。
4. **数据外泄与合规**：资料抓取与市场请求都出网（默认开启）。对策：`nuwa_knowledge_online` 可单独关闭、域名白名单可收紧、`robots.txt`、单域限速、正文大小上限、审计记录目标域名与条数；抓取内容仅入企业知识库，不回传任何内部数据。
5. **出网开关语义混淆**：管理员可能以为关掉 `mcp_egress_enabled` 就等于全平台断网。对策：两个开关在界面上分区展示、文案互引（见 5.1），设置页写明"互不影响"。
6. **成本**：每会话 1–3 次 LLM 调用 + 一次向量化 + 市场与抓取网络请求。对策：`nuwa_daily_session_limit`（默认 20/人/日）、brief 长度上限 2000 字、单次抓取 ≤20 条。
7. **误导性"智能"**：候选全部来自真实目录并带来源链接与分数理由；抓不到就空态说明，不生成假条目（实现时禁止任何 fallback 假数据）。
8. **与既有灰度/可见范围的关系**：女娲产出的技能沿用 `Skill.scope_mode/rollout_percent` 默认（全员、100%）——**创建时不自动改灰度**，避免绕过既有投放口径。

---

## 13. 已拍板

**C 方案 + 在线优先**（2026-11-14 确认）：

- 前提是允许平台访问互联网；`nuwa_market_online` 与 `nuwa_knowledge_online` 默认开启，检索与抓取默认直连公网，结果同时写本地缓存。
- 本地目录（`nuwa_sync` + `market-cache.json`）保留为兜底与离线包：网络失败、超时、被墙或管理员关闭开关时自动回落，标注"同步于 X"，不报错、不空白。
- 出网边界：女娲的两把开关与 `mcp_egress_enabled` 完全独立，互不影响（见 5.1）。
- 安全与审批口径不变：外部条目单级审批、MCP 落库 `unknown` 且工具全关、技能落库待审核、产物固定草稿。

实现顺序（不 push、不部署）：
`MarketItem/MarketSource + nuwa_sync` → 骨架生成 → 匹配打分 → 技能/MCP 审批导入 → 知识抓取入库 → 五步前端 → 本地提交。

---

## 14. 实现落地（已完成）

### 14.1 后端

| 文件 | 内容 |
|---|---|
| `backend/apps/nuwa/models.py` | `MarketSource / MarketItem / MarketSyncRun / NuwaSession / NuwaImportRequest` |
| `backend/apps/nuwa/config.py` | 两把女娲开关（默认开）+ 域名白名单 + 每日会话上限，变更入审计 |
| `backend/apps/nuwa/net.py` | 标准库出网层：固定超时、有限重试、失败归类、https/公网校验、certifi CA 兜底 |
| `backend/apps/nuwa/market.py` | registry / GitHub 技能目录解析与归一化、安全校验、缓存 upsert、在线优先检索 + 兜底、离线包导入导出 |
| `backend/apps/nuwa/generation.py` | 骨架生成（模板 `v1`、JSON 契约、1 次修复重试、空白兜底、占位符白名单） |
| `backend/apps/nuwa/imports.py` | 外部条目草稿、风险标记、单级审批落库（MCP `unknown`+工具全关；技能 `pending`） |
| `backend/apps/nuwa/knowledge_fetch.py` | robots、同域限速、正文清洗、临时文件、去重（URL + 内容指纹）、入库并同步向量化 |
| `backend/apps/nuwa/views.py` / `urls.py` | 会话 10 个动作 + 市场源/条目/审批/开关，见第 10 节 |
| `backend/apps/nuwa/management/commands/nuwa_sync.py` | 在线同步 / 导出离线包 / 断网导入 |
| `backend/apps/agenthub` | `AgentProfile.welcome_message`，并纳入 `snapshot()` 与差异分区 |
| `backend/apps/knowledge` | `Document` 新增 `source_url/source_title/source_site/source_license/fetched_at/content_sha256` + 同库来源唯一约束 |

### 14.2 前端

- `frontend/src/api/nuwa.ts`：会话、检索、资料、审批、开关的类型与调用封装。
- `frontend/src/pages/Agents/Nuwa/index.tsx`：五步向导，逐步可回退；市场检索显示"实时 / 缓存 · 同步于 X"与降级原因；资料步骤逐条状态与勾选。
- `frontend/src/pages/Agents/index.tsx`：页头新增「女娲模式」入口，生成后直接打开装配页。

### 14.3 实测结果（本地 SQLite + 真实公网）

| 场景 | 结果 |
|---|---|
| 官方 registry 在线同步（query=postgres） | 成功入库，字段含 transport/egress_domains |
| 市场检索 | `mode=online`，返回 12 条，带分数与命中理由 |
| 关闭 `nuwa_market_online` | 自动回落 `mode=cached`、`degraded_reason=switch_off`，仍返回缓存条目，无报错 |
| LLM 未配置时生成骨架 | 返回可编辑空白骨架 + 明确错误，不编造字段，接口 201 |
| 技能目录 + 正文解析 | `SKILL.md` frontmatter（含 `description: >` 块标量）解析正常 |
| 技能导入审批 | 正文 7715 字，落库 `pending`，未审核不可挂载 |
| MCP 导入审批 | 落库 `status=unknown`、`enabled_tools=[]`；因平台工具出网关闭显示 `egress_blocked`，符合两开关独立 |
| 资料抓取（example.com） | 逐条 `ok`，标题/字数/指纹/临时文件正常 |
| 资料入库 + 去重 | 首轮创建文档、写来源字段；同 URL 再提交被跳过并给原因 |
| 女娲建草稿 | 201，返回 `agent_id`、挂载情况、待审批项与警告 |
| 前端 `tsc -b && vite build` | 通过，入口 362 kB（gzip 99 kB） |

### 14.4 已知限制

- 首版"资料检索"是"给定 URL + 同域浅层"，不是全网搜索；接第三方搜索 API 需另外决策。
- 技能只接收单文件 `SKILL.md`；含脚本/附件的技能包需人工上传。
- 未配置模型接入时女娲只能产出空白骨架（符合"不编造"）。
- 在线检索的词是需求收敛后的单关键词（英文优先），中文长句的召回仍依赖本地缓存与关键词打分。
