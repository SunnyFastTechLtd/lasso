# Lasso API 接口文档

> 生成依据：`backend/urls.py`、各应用 `urls.py`、`views.py`、`serializers.py` 的实际实现（核对时间 2026-11-08）。
> 不照抄代码注释；路径、方法、权限类、请求/响应字段均以路由注册与视图逻辑为准。
> 本文不含任何真实口令、token、密钥；示例一律用占位符 `<access>`、`<refresh>`、`<id>`。
> 标注 **待核实** 的条目表示未能从源码完整确认，使用前需再核对。

## 0. 通用约定

| 项 | 约定 |
|---|---|
| 统一前缀 | `/api/`，JSON 请求与响应；导出、下载、SSE 除外 |
| 生产入口 | nginx 80 端口反代到 `127.0.0.1:8000`；后端直连见 `docs/deploy-runbook.md` |
| 认证方式 | 请求头 `Authorization: Bearer <access>`（SimpleJWT，HS256） |
| access 有效期 | 默认 60 分钟（`JWT_ACCESS_TOKEN_LIFETIME_MINUTES`） |
| refresh 有效期 | 默认 7 天；登录时 `remember=true` 延长为 30 天 |
| refresh 轮换 | `ROTATE_REFRESH_TOKENS=True`、`BLACKLIST_AFTER_ROTATION=True`，刷新后旧 refresh 立即拉黑 |
| `/media/` 认证 | 不走 Bearer，走 HttpOnly Cookie `media_token`（7 天），由 nginx `auth_request` 调 `/api/media/auth/` 校验 |
| 分页 | `StandardPagination`：查询参数 `page`、`page_size`（默认 20，上限 100） |
| 分页响应 | `{"count", "page", "page_size", "total_pages", "results"}` |
| 时间格式 | ISO 8601 字符串，服务端时区 `Asia/Shanghai` |
| 错误响应 | 一般为 `{"detail": "..."}`；字段校验错误为 `{"字段名": ["..."]}` |

常用状态码：

| 状态码 | 含义 |
|---|---|
| 400 | 参数校验失败 / 业务前置条件不满足 |
| 401 | 未登录、token 失效或口令错误 |
| 403 | 已登录但无权限（含非管理员访问管理端接口） |
| 404 | 资源不存在，或资源对当前用户不可见（故意不区分，避免探测） |
| 409 | 状态冲突（已发布/被引用/授权已处理等） |
| 410 | 资源已过期（授权请求超时、生成文件过期） |
| 413 / 415 | 文件过大 / 文件类型不支持 |
| 422 | 命中高危内容，需要显式风险确认后重试 |
| 429 | 登录失败次数超阈值 |
| 503 | 模型未配置或不可用、导出依赖缺失 |

## 1. 认证与账号（`/api/auth/`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/auth/login/` | 匿名 | 登录，返回 token 对，并下发 `media_token` Cookie |
| POST | `/api/auth/register/` | 匿名 | 自助注册；`ALLOW_REGISTER=False`（出厂默认）时固定 403 |
| POST | `/api/auth/refresh/` | 匿名 | 用 refresh 换新 token 对（标准序列化器，轮换 + 拉黑） |
| POST | `/api/auth/logout/` | 登录用户 | 拉黑提交的 refresh，并清除 `media_token` Cookie |
| GET | `/api/auth/profile/` | 登录用户 | 当前用户资料 |
| PUT / PATCH | `/api/auth/profile/` | 登录用户 | 修改本人资料（部分字段只读） |
| POST | `/api/auth/change-password/` | 登录用户 | 修改本人密码，成功后吊销全部 refresh 并为当前设备重签 |
| GET / POST | `/api/auth/manage/` | 管理员 | 用户列表 / 新建用户 |
| GET / PUT / PATCH / DELETE | `/api/auth/manage/{id}/` | 管理员 | 用户详情 / 编辑 / 软删除 |
| GET | `/api/auth/manage/{id}/sessions/` | 管理员 | 该用户当前活跃登录（基于 OutstandingToken） |
| POST | `/api/auth/manage/{id}/revoke-sessions/` | 管理员 | 强制下线（只吊销令牌，不改账号启停） |
| GET / POST | `/api/auth/departments/` | 管理员 | 部门列表 / 新建 |
| GET / PUT / PATCH / DELETE | `/api/auth/departments/{id}/` | 管理员 | 部门详情 / 编辑 / 删除 |
| GET | `/api/auth/audit-logs/` | 管理员 | 审计日志列表 |
| GET | `/api/auth/audit-logs/{id}/body/` | 管理员 | 查看某条工具调用返回全文（查看即留痕） |

### 1.1 登录

`POST /api/auth/login/`

```json
{"username": "<username>", "password": "<password>", "remember": false}
```

成功 200：

```json
{
  "access": "<access>",
  "refresh": "<refresh>",
  "user": {"id": 1, "username": "<username>", "nickname": "", "email": "", "phone": "",
           "department": "", "position": "", "is_staff": false,
           "date_joined": "2026-01-01T00:00:00+08:00",
           "total_chat_count": 0, "total_tokens_used": 0, "storage_used": 0}
}
```

- 同时返回 `Set-Cookie: media_token=...; HttpOnly; SameSite=Lax; Max-Age=604800`。
- 失败：口令错误 401；同一「用户名（忽略大小写）+ IP」失败达到阈值（默认 5 次 / 300 秒）后返回 429，锁定期内正确口令同样 429。
- 登录成功会把 `last_login` 写回，并记录 `login_success` 审计；失败与锁定记录 `login_failed`。

### 1.2 注册

`POST /api/auth/register/`，字段：`username`、`password`、`confirm_password`、`nickname`、`email`。

- `ALLOW_REGISTER` 未开启时返回 403 `{"detail": "注册接口未开放，请联系管理员创建账号"}`。
- 开启后成功返回 201，结构同登录。

### 1.3 刷新

`POST /api/auth/refresh/`，请求 `{"refresh": "<refresh>"}`。

- 返回 `{"access": "<access>", "refresh": "<new_refresh>"}`（轮换开启时一定带新 refresh），并重下 `media_token` Cookie。
- 失效/已拉黑/过期返回 401。

### 1.4 登出

`POST /api/auth/logout/`，请求可带 `{"refresh": "<refresh>"}`。

- refresh 合法则拉黑；解析失败也返回成功（幂等）。
- 响应 `{"detail": "登出成功"}`，并清除 `media_token` Cookie。

### 1.5 当前用户资料

`GET /api/auth/profile/` 返回 UserSerializer 字段：

`id, username, nickname, email, phone, department, position, is_staff, date_joined, total_chat_count, total_tokens_used, storage_used`。

`PUT/PATCH` 可改：`nickname`、`email`、`phone`、`position`。
只读：`id`、`username`、`is_staff`、`department`、`date_joined`、三项用量统计。

### 1.6 修改密码

`POST /api/auth/change-password/`

```json
{"old_password": "<old>", "new_password": "<new>", "confirm_password": "<new>"}
```

- 原密码错误 400；新密码强度走 Django `validate_password`（与 `AUTH_PASSWORD_VALIDATORS` 同口径）。
- 成功 200：`{"detail": "密码修改成功", "access": "<access>", "refresh": "<refresh>"}`；其它设备的 refresh 全部拉黑，当前设备拿到新 token。

### 1.7 用户管理（管理员）

- 列表参数：`search`（用户名/昵称/邮箱/手机号模糊）、`deleted=1|true|yes`（查看已软删除账号）、`page`、`page_size`。
- 新建/编辑字段：`username`、`password`（仅写入；PATCH 带 `password` 即管理员重置密码，会吊销该用户全部 refresh 并写 `password_reset` 审计）、`nickname`、`email`、`phone`、`department`、`position`、`is_staff`、`is_active`；只读 `is_superuser`、`is_last_admin`、`is_deleted`、`deleted_at`、`deleted_by_name`、`last_login`、`date_joined`。
- 删除为软删除：置 `is_deleted=true`、`is_active=false`，吊销全部 refresh，返回 204；历史会话与审计保留。
- 保护规则：不能删除自己；不能让系统失去最后一个「`is_active=True` 且 `is_staff` 或 `is_superuser`」的账号（降级/停用/删除同一口径）。
- `department` 必须是已存在的部门名称（待核实：具体校验错误文案）。

`sessions` 响应：

```json
{
  "count": 1,
  "results": [{"id": 1, "jti": "<jti>", "created_at": "...", "expires_at": "...",
               "last_used": null, "ip": "x.x.x.x", "user_agent": "...",
               "source_action": "login_success", "source_note": "..."}],
  "last_used_supported": false,
  "last_used_note": "...",
  "correlation_note": "..."
}
```

`revoke-sessions` 响应：`{"revoked": <number>, "detail": "..."}`。

### 1.8 部门（管理员）

字段：`id, name, description, created_at`。`name` 必填且唯一（去首尾空格）。
`parent` 模型上有层级字段，但**序列化器未暴露**，当前接口不能读写上级部门。

### 1.9 审计日志（管理员）

- 参数：`actor`（用户名/昵称模糊）、`action`（精确）、`target`（模糊）、`date_start`、`date_end`（`YYYY-MM-DD`，闭区间）、`page`、`page_size`。
- 字段：`id, actor, actor_name, action, action_display, target, detail, ip, user_agent, created_at, has_body`。
- `GET /audit-logs/{id}/body/`：返回工具调用全文及 `storage_truncated`、`result_sha256` 等；无全文或已过 90 天保留期时 404 且返回 `code`；成功查看会写 `tool_body_view` 审计。

## 2. 对话（`/api/chat/`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET / POST | `/api/chat/conversations/` | 登录用户 | 会话列表（仅本人）/ 新建会话 |
| GET / PUT / PATCH / DELETE | `/api/chat/conversations/{id}/` | 登录用户 | 会话详情 / 更新 / 删除（仅本人） |
| GET | `/api/chat/conversations/{id}/messages/` | 登录用户 | 消息列表（分页） |
| POST | `/api/chat/conversations/{id}/delete_conversation/` | 登录用户 | 删除会话（与 DELETE 等价） |
| GET | `/api/chat/conversations/{id}/export/` | 登录用户 | 导出，`type=md|docx|pdf` |
| POST | `/api/chat/chat/upload_attachment/` | 登录用户 | 上传对话临时附件并提取文本 |
| POST | `/api/chat/chat/send/` | 登录用户 | 发送消息（流式 / 非流式） |
| POST | `/api/chat/chat/trial/` | 登录用户（仅创建者或管理员可对指定智能体试聊） | 试聊草稿配置，不落会话 |
| POST | `/api/chat/chat/regenerate/` | 登录用户 | 重新生成上一版回答 |
| POST | `/api/chat/chat/permission/` | 登录用户 | D7 授权卡片决定入口 |
| GET | `/api/chat/models/health/` | 登录用户 | 模型连通性状态 |
| GET | `/api/chat/models/config/` | 登录用户 | 当前模型非敏感配置 |
| GET | `/api/chat/files/{id}/download/` | 登录用户 | 下载本人 AI 生成文件 |

### 2.1 会话

`ConversationSerializer` 字段：`id, title, type, resource_id, knowledge_base_ids, agent, agent_name, agent_status, message_count, last_message_at, created_at, updated_at`。
可写：`title`、`type`、`resource_id`、`agent`。列表参数：`type`、`page`、`page_size`。
**会话按 `user=request.user` 过滤，管理员也不能通过本接口读取他人会话。**

`MessageSerializer` 字段：`id, role, content, meta, attachments, created_at`。

### 2.2 上传附件

`POST /api/chat/chat/upload_attachment/`，`multipart/form-data`，字段名 `file`。

- 支持 `txt/md/pdf/docx/xlsx/pptx`；单文件上限 10 MB；提取文本超过 6000 字符时截断。
- 成功 200：`{"name", "size", "type", "text", "truncated"}`；该对象可原样放进 `send` 的 `attachments` 数组。
- 413 文件过大、415 类型不支持、422 解析失败。

### 2.3 发送消息

`POST /api/chat/chat/send/`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `message` | string（≤8000） | 是 | 用户问题 |
| `conversation_id` | int | 否 | 不传则新建会话 |
| `type` | `chat` / `knowledge` | 否 | 默认 `chat` |
| `resource_id` | int | 否 | 知识库等资源标识 |
| `agent_id` | int | 否 | 本次使用的智能体；不传沿用会话已绑定 |
| `knowledge_base_ids` | int[]（≤8） | 否 | 本轮勾选的知识库 |
| `stream` | bool | 否 | 默认 `false` |
| `temperature` | number | 否 | 覆盖采样温度 |
| `attachments` | object[] | 否 | 上传接口返回值原样回传 |

非流式成功 200：

```json
{"conversation_id": 1, "answer": "...", "generated_files": [],
 "sources": [], "degrade": []}
```

流式见第 10 节。模型未配置时返回 503。

### 2.4 试聊

`POST /api/chat/chat/trial/`

```json
{"agent_id": 1, "message": "...", "messages": [{"role": "user", "content": "..."}]}
```

- 仅 `can_edit` 通过者（创建者或管理员）可用，否则 403 `code=forbidden`。
- 不创建会话、不写消息；工具调用写审计且标记来源「试聊」，不计入 F4 用量。
- 固定流式返回，事件见第 10 节。

### 2.5 重新生成

`POST /api/chat/chat/regenerate/`，字段 `conversation_id`（必填）、`message_id`（可选）。
前端传了 `message_id` 但服务端最新一版不一致时报错，不做静默重生成。返回流式，事件同 `send`。

### 2.6 授权决定（D7）

`POST /api/chat/chat/permission/`

```json
{"request_id": "<request_id>", "decision": "allow_once|allow_session|deny"}
```

| 结果 | 状态码 | 响应 |
|---|---|---|
| 成功 | 200 | `{"code": "ok"}` |
| 已超时（固定 60 秒，按拒绝处理） | 410 | `{"detail": "...", "code": "permission_expired"}` |
| 已处理 | 409 | `{"code": "permission_already_resolved"}` |
| 不存在/失效 | 404 | `{"code": "permission_not_found"}` |
| 参数不合法 | 400 | `{"code": "invalid_permission_request"}` |

接口只接受请求标识与决定，不接受工具名或参数，避免借授权接口扩权。

### 2.7 模型状态

- `GET /api/chat/models/health/`：返回 `check_health()` 结果（含 `status`，异常时含 `error`）。
- `GET /api/chat/models/config/`：`{"base_url_display", "model", "temperature", "max_tokens", "is_local"}`，隐藏主机名与密钥。

### 2.8 生成文件下载

`GET /api/chat/files/{id}/download/`：仅文件属主可下载；过期返回 410，文件缺失 404。

## 3. 知识库（`/api/knowledge/`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET / POST | `/api/knowledge/bases/` | 登录用户 | 可见知识库列表 / 新建 |
| GET / PUT / PATCH / DELETE | `/api/knowledge/bases/{id}/` | 登录用户（写操作需属主或管理员） | 详情 / 编辑 / 删除 |
| GET | `/api/knowledge/bases/{id}/stats/` | 登录用户（可见即可读） | 统计信息 |
| GET / POST | `/api/knowledge/documents/` | 登录用户 | 文档列表（`knowledge_base_id` 过滤）/ 新建 |
| GET / PUT / PATCH / DELETE | `/api/knowledge/documents/{id}/` | 登录用户（写操作需属主或管理员） | 文档详情 / 编辑 / 删除 |
| POST | `/api/knowledge/documents/upload/` | 属主或管理员 | 上传文档（multipart） |
| DELETE | `/api/knowledge/documents/{id}/delete/` | 属主或管理员 | 删除文档（等价 DELETE，并清向量） |
| POST | `/api/knowledge/documents/{id}/reprocess/` | 属主或管理员 | 重新解析 / 分块 / 向量化 |
| GET | `/api/knowledge/documents/{id}/chunks/` | 可见即可读 | 分块列表 |
| PATCH | `/api/knowledge/documents/{id}/chunks/{chunk_id}/` | 属主或管理员 | 切换分块可检索状态 |
| POST | `/api/knowledge/chat/ask/` | 登录用户（知识库需可见） | 知识库问答（流式 / 非流式） |

### 3.1 知识库

字段：`id, name, description, icon, color, is_public, scope, department, document_count, chunk_count, size_bytes, created_at, updated_at`。

- `scope` 取 `public / department / private`，是权威字段；未传 `scope` 时由 `is_public` 映射（`true→public`，`false→private`）。
- `scope=department` 必须给 `department`，否则 400。
- 列表对非管理员按「公开 + 自己创建 + 本部门」过滤；管理员全量。
- 写操作（改/删）仅属主或管理员；部门共享库对同部门成员是只读。

### 3.2 文档

字段：`id, knowledge_base, knowledge_base_name, title, file_name, file_type, file_size, size_human, file_url, status, error_msg, chunk_count, uploaded_by_name, created_at, updated_at`。

- 上传为 `multipart/form-data`：`file`（必填）、`knowledge_base_id`（必填）、`title`（可选，缺省取文件名）。
- 写入只能落在 `can_manage_kb` 通过的知识库下。
- 删除会清理向量并同步知识库计数。

分块字段：`id, chunk_index, content, is_active, has_embedding, created_at`；`PATCH chunks/{chunk_id}/` 传 `is_active` 切换检索开关。

### 3.3 知识库问答

`POST /api/knowledge/chat/ask/`

```json
{"question": "...", "knowledge_base_id": 1, "stream": false}
```

非流式 200：

```json
{"answer": "...", "sources": [{"title": "...", "score": 0.82, "chunk_id": 1,
  "doc_id": 1, "chunk_index": 0, "content": "..."}],
 "knowledge_base": {"id": 1, "name": "..."}}
```

流式事件：`start`（含知识库与 `sources`）→ `chunk` → `end`（含 `answer_length`），异常时 `error` + `end`。知识库不可见时 404。

## 4. 智能体装配（`/api/agents/`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET / POST | `/api/agents/` | 登录用户 | 列表 / 新建 |
| GET / PUT / PATCH / DELETE | `/api/agents/{id}/` | 登录用户（写操作需创建者或管理员） | 详情 / 更新 / 删除 |
| GET / POST | `/api/agents/{id}/publish-check/` | 创建者或管理员 | 发布前检查清单 + 与线上版本差异 |
| POST | `/api/agents/{id}/publish/` | 创建者或管理员 | 发布（`risk_ack`、`confirm_diff` 按需） |
| POST | `/api/agents/{id}/disable/` | 创建者或管理员 | 停用 |
| POST | `/api/agents/{id}/enable/` | 创建者或管理员 | 恢复 |
| POST | `/api/agents/{id}/archive/` | 创建者或管理员 | 归档 |
| POST | `/api/agents/{id}/restore/` | 创建者或管理员 | 归档 → 草稿 |
| POST | `/api/agents/{id}/restore-builtin/` | 管理员（待核实：视图仅走 `_require_edit`，内置件非创建者不可编辑，实际等于管理员） | 内置智能体恢复出厂草稿 |
| POST | `/api/agents/{id}/clone/` | 登录用户 | 复制新建（产物为草稿） |
| GET | `/api/agents/{id}/versions/` | 可见即可读 | 发布历史（分页） |
| GET | `/api/agents/{id}/versions/{n}/` | 可见即可读 | 某版本完整配置 |
| GET | `/api/agents/{id}/versions/compare/` | 可见即可读 | 任选两版本对比 |
| POST | `/api/agents/{id}/versions/{n}/rollback/` | 创建者或管理员 | 以该版本重新发布（生成新版本号） |
| GET | `/api/agents/{id}/mount-options/` | 可见即可读 | 四分区候选 |
| GET | `/api/agents/options/` | 登录用户 | 模型 / 档位 / 内置能力 / 占位符候选 |
| GET | `/api/agents/summary/` | 登录用户 | 顶部统计与空状态 |

- 列表参数：`search`、`status`（可逗号分隔）、`visibility`、`job_tag`；默认排除已归档。
- 权限类均为 `IsAuthenticated`，**对象级权限在视图内判定**：详情不可见返回 404，写操作要求 `created_by=本人且非内置` 或管理员（内置件只能「复制新建」）。
- 写字段（`AgentWriteSerializer`）：`name, description, job_tags, icon, model_name, speech_tier, retrieval_tier, enable_thinking, prompt, mcp_mounts, skill_ids, knowledge_base_ids, builtin_capabilities, permission_tier, allow_tool_names, visibility, allowed_department_ids, include_sub_departments`。
- 详情额外下发：`status`、`current_version`、`pending_changes`、`tool_conflicts`、`mount_summary`、`permission_tier_display`、`visibility_display`、创建/修改/发布人、时间戳。
- 发布：`publish-check` 需 `requires_ack` 而请求未带 `risk_ack` 时返回风险确认；非首次发布必须先确认已看过差异（`confirm_diff`）。
- 版本回滚会重跑 A7 检查、生成新版本号，不覆盖历史。

## 5. MCP 工具来源（`/api/mcp/`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET / POST | `/api/mcp/servers/` | 登录（读）/ 管理员（写） | 列表 / 新增 |
| GET / PUT / PATCH / DELETE | `/api/mcp/servers/{id}/` | 登录（读）/ 管理员（写） | 详情 / 更新 / 删除 |
| POST | `/api/mcp/servers/{id}/test/` | 管理员 | 连通性测试 + 工具发现 |
| GET | `/api/mcp/servers/{id}/tools/` | 登录 | 工具清单 |
| POST | `/api/mcp/servers/{id}/tools/toggle/` | 管理员 | 工具启停 |
| POST | `/api/mcp/servers/{id}/disable/`、`/enable/` | 管理员 | 停用 / 恢复 |
| GET | `/api/mcp/servers/{id}/references/` | 登录 | 被哪些智能体引用 |
| GET | `/api/mcp/servers/command-prefixes/` | 管理员 | 内置只读命令白名单 |
| GET / POST | `/api/mcp/servers/egress/` | 管理员 | 出网总开关（POST `{"enabled": bool}`） |
| GET | `/api/mcp/servers/mountable/` | 登录 | 装配页选择器 |

> 注意：`command-prefixes` 与 `egress` 虽然是 GET，但被列在视图的 `WRITE_ACTIONS` 中，实际要求管理员。
> `test`、`toggle_tools` 同样是管理员权限，而非「登录即可」。

- 列表参数：`search`、`status`、`transport`、`page`、`page_size`。
- 字段：`id, name, slug, description, transport, url, headers, command, args, cwd, secrets, secrets_masked, has_secrets, is_stateful, lifecycle, timeout_seconds, effective_timeout, timeout_default, enabled_tools, discovered_tools, tool_count, enabled_tool_count, new_tool_count, agent_count, status, last_check_at, last_error, requires_egress, is_builtin, is_active, visibility, allowed_department_ids, allowed_departments, created_by_name, created_at, updated_at`。
- `secrets` 写入掩码值表示「不修改」；`slug`、`discovered_tools`、`status`、`last_check_at`、`last_error`、`requires_egress`、`is_builtin`、`is_active` 只读。
- `toggle_tools` 请求支持 `{"names": [...], "enabled": true}`、`{"tool_name": "...", "enabled": true}`，或 `{"select_all": true}` / `{"invert": true}`。
- 删除有引用时默认 409（`code=mcp_server_referenced`，带 `references` 与可选项）；只有 `DELETE /api/mcp/servers/{id}/?confirm_unmount=true` 才会摘除挂载并删除，成功后 204。内置来源不可删除，只能停用。
- `test`、`disable`、`enable`、`toggle_tools` 都会写审计。
- 非管理员列表按「公开 + 自己创建 + 本部门及上级部门授权」过滤。

## 6. 技能（`/api/skills/`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET / POST | `/api/skills/` | 登录（读）/ 管理员（写） | 列表 / 上传建档 |
| GET / PATCH / DELETE | `/api/skills/{id}/` | 登录（读）/ 管理员（写） | 详情 / 编辑 / 删除 |
| GET / POST | `/api/skills/{id}/files/` | 登录（读）/ 管理员（写） | 附件清单 / 批量上传 |
| DELETE | `/api/skills/{id}/files/{file_id}/` | 管理员 | 删除附件 |
| GET | `/api/skills/{id}/files/{file_id}/download/` | 登录 | 下载附件 |
| GET | `/api/skills/{id}/versions/` | 登录 | 版本列表 |
| GET | `/api/skills/{id}/versions/{n}/` | 登录 | 版本详情 |
| GET | `/api/skills/{id}/versions/compare/` | 登录 | 版本对比 |
| POST | `/api/skills/{id}/versions/{n}/rollback/` | 管理员 | 回滚到该版本 |
| GET | `/api/skills/{id}/versions/{n}/files/{file_id}/download/` | 登录 | 下载历史版本附件 |
| POST | `/api/skills/{id}/approve/` | 管理员 | 审核通过 |
| POST | `/api/skills/{id}/reject/` | 管理员 | 驳回（`reason` 必填） |
| POST | `/api/skills/{id}/disable/`、`/enable/` | 管理员 | 停用 / 恢复启用 |
| GET | `/api/skills/{id}/references/` | 登录 | 被哪些智能体挂载 |
| GET | `/api/skills/options/` | 登录 | 装配页选择器 |
| GET | `/api/skills/summary/` | 登录 | 各状态计数 |
| GET / POST | `/api/skills/mirror/` | 管理员 | 运行时镜像可见性 / 全量重建 |

> `mirror` 两个方法都在 `WRITE_ACTIONS` 中，GET 也要求管理员。`files`、`delete_file`、`rollback` 同样按管理员处理。

- 列表参数：`search`、`status`、`source`、`page`、`page_size`；列表不返回正文（`compact`）。
- 非管理员列表按「全企业 + 自己创建 + 命中部门（含上级部门授权）」过滤；**未按 `status` 过滤**，停用/待审技能仍会出现在列表里（运行期是否可用由装配页 `mountable` 判断）。
- 字段：`id, name, slug, description, source, source_display, entry_file, status, status_display, char_count, has_body, body, approved_body, body_changed, validation_errors, reject_reason, scan_warnings, requires, mountable, live, editable, visibility, visibility_display, allowed_department_ids, allowed_departments, scope_mode, scope_mode_display, scope_include_sub_departments, applicable_department_ids, applicable_departments, current_version, files, file_count, package_bytes, file_scan_warnings, risk_ack, agent_count, reviewed_by_name, reviewed_at, mirror_path, created_at, updated_at`。
- 审核通过与驳回都必须带「已看过正文」凭证：`body_ack=true` + `body_hash`（当前正文指纹）；正文在审核期间变更会要求重新查看。
- 命中高危内容时返回 422，带命中规则、位置与 `risk_fingerprint`；带匹配的 `risk_ack=true` + `risk_fingerprint` 重试才落库，并记录确认人、规则集版本。
- 附件上传受系统设置项「技能包附件类型」限制，`files` 接口支持 `files[]` + 路径。

## 7. 系统设置（`/api/system/`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/system/branding/` | 匿名 | 企业品牌（站点名、Logo） |
| POST | `/api/system/branding/` | 管理员 | 更新品牌（支持 Logo 上传） |
| GET | `/api/system/settings/llm/` | 登录用户 | 模型配置（密钥掩码） |
| POST | `/api/system/settings/llm/update/` | 管理员 | 先探测候选配置，通过才落库 |
| POST | `/api/system/settings/llm/test/` | **仅登录用户** | 用已保存配置探测，成功后把模型写入 verified_models |
| POST | `/api/system/settings/llm/reset/` | 管理员 | 清空数据库模型配置 |
| GET | `/api/system/settings/skill-package/` | 登录用户 | 技能包附件类型与限制 |
| POST | `/api/system/settings/skill-package/update/` | 管理员 | 修改附件类型白名单 |

> 安全事实：`llm/test/` 只要求登录，任意登录用户都能触发一次模型探测，并可能改变 `verified_models`（模型下拉候选）。是否收紧待产品决策，当前代码如此。

`GET llm/` 响应：

```json
{"base_url": "...", "api_key_masked": "sk-***xyz", "model": "...",
 "temperature": 0.7, "max_tokens": 4096, "timeout": 120,
 "verified_models": ["..."], "config_source": "database",
 "configured": true, "missing_fields": [], "hint": ""}
```

`llm/update/` 请求：`base_url`、`api_key`、`model`、`temperature`、`max_tokens`、`timeout`。
探测失败返回 200 但 `saved=false`，现网配置不变。`llm/reset/` 清空后平台会明确拒绝调用模型，不回退环境变量。

## 8. 用量（`/api/usage/`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/usage/overview/` | 登录用户 | 用量概览（只聚合，不下钻） |

参数：`granularity=day|week`、`start`、`end`（`YYYY-MM-DD`）、`scope=all|dept`、`department`。

- 默认区间：`end` 起往前 30 天；最长 366 天。
- `scope=all` 仅管理员可用（否则 403）；`scope=dept` 时非管理员只能查自己部门，未归口部门返回 403。
- 响应含 `scope`、`department`、`granularity`、`start`、`end`、`metrics`、`series`、`definitions`；数据库异常时返回空看板并带 `error`，不抛 500。

## 9. `/api/media/auth/`

nginx `auth_request` 子请求入口，**不参与 JWT 认证**，由 HttpOnly Cookie `media_token` 判定。

- 200：允许访问（属主本人、公开知识库文档、或 `branding/` 公开目录；`branding/` 未登录也返回 200）。
- 401：未登录、Cookie 失效或账号已停用。
- 403：路径不安全（路径穿越、双重编码、隐藏文件、目录枚举，**未登录也直接 403**），或已登录但无权访问（他人私有文件、`skills/` 等内部镜像目录）。
- 405：非 `GET` / `HEAD` 请求。
- 请求 URI 取 nginx 传入的 `X-Original-URI`，缺失时退化为查询参数 `uri`。

路径规则与实测结果见 `docs/deploy-runbook.md` 7.4 / 9.2。

## 10. 流式协议（SSE）

### 10.1 帧格式

```
event: <event_name>
data: <JSON 字符串>

```

- `Content-Type: text/event-stream`，响应头带 `Cache-Control: no-cache`、`X-Accel-Buffering: no`。
- 等待授权期间的保活是 SSE 注释行 `: keep-alive\n\n`，前端解析器应忽略。
- `data` 一律是 JSON；`error` / `end` 在部分路径下可能是字符串（如 `end` 为 `""`），解析时需容忍。

### 10.2 `POST /api/chat/chat/send/`（`stream=true`）

事件顺序：

1. `start` —— `{"conversation_id", "message_id", "sources": [...]}`（模型不可用时不会出现，直接 `error` + `end`）
2. 可选 `degrade` —— `{"notes": string[]}`，降级说明；多数先于正文，A3 触顶时会在正文后补发一条，两次的 `notes` 需累加而非覆盖
3. 生成期间循环出现：
   - `chunk` —— `{"content": "..."}`
   - `thinking` —— 思考过程（结构随运行时，**待核实**字段）
   - `tool` / `tool_args` / `tool_result` —— 工具调用状态、参数、结果（同一 `data` 结构由运行时给出，**待核实**具体键）
   - `permission_request` —— D7 授权请求（含请求标识、工具展示名等）
   - `permission_result` —— 授权结果（含 `decision`）
   - `: keep-alive` 注释
4. `end` —— `{"message_count": n, "generated_files": [...]}`
5. 异常时：`error`（`{"message": "..."}` 或原文）→ `end`（可能为空串）

模型不可用时先发 `error` + `end` 并不落消息。

### 10.3 `POST /api/chat/chat/trial/`

固定流式，顺序：

1. `start` —— `{"thread_id": "..."}`
2. `chunk` / `degrade` / `thinking` / `tool` / `tool_args` / `tool_result` / `permission_request` / `permission_result` / keep-alive 注释，按运行时产生
3. `done` —— 试聊结束（注意不是 `end`）
4. 异常：`error` 后直接结束，不发 `done`

### 10.4 `POST /api/knowledge/chat/ask/`（`stream=true`）

1. `start` —— `{"knowledge_base": {"id", "name"}, "sources": [...]}`
2. `chunk` —— `{"content": "..."}`
3. `end` —— `{"answer_length": n}`；异常时 `error` + `end`

### 10.5 通用建议

- 前端不能用「收到 `chunk`」判断开始，必须以 `start` 为准。
- 客户端主动中断（停止生成 / 关页）时，服务端会把已生成部分落库并标记 `meta.stopped=true`；若授权等待中断且无正文，额外标记 `meta.permission_interrupted=true`。
- 生产 nginx 已关闭 `proxy_buffering`，否则 SSE 会被整段缓冲。

## 11. 待核实清单

| 项 | 原因 |
|---|---|
| `thinking` / `tool` / `tool_args` / `tool_result` 的具体字段 | 由 `backend/apps/agents` 运行时产生，本文只确认了事件名与透传行为 |
| `publish-check` 返回的三档结论完整结构 | 视图调用 `services` 计算，未逐字段核对 |
| `thinking` 事件在通用助手（未绑定智能体）路径下是否出现 | 由 AgentScope 运行时决定 |
| 用户管理对 `department` 存在性的具体校验与错误文案 | 序列化器未展示该校验，可能在视图补充 |
| `restore-builtin` 的实际权限边界 | 视图只走 `_require_edit`，内置件创建者非普通用户，等效管理员 |
| `end` 事件在异常路径下为字符串的兼容范围 | 代码同时存在 `format_sse_event('end', '')` 与对象两种调用 |

---

关联文档：[认证与权限矩阵](auth-permission-matrix.md) · [备份与恢复记录模板](backup-restore-template.md) · [部署与回滚 Runbook](deploy-runbook.md)
