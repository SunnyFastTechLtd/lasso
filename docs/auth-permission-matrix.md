# Lasso 认证与权限矩阵

> 依据：`backend/settings.py` 的 DRF 全局配置，以及各 `views.py` 的 `permission_classes` / `get_permissions` / 对象级判定实现（核对时间 2026-11-08）。
> 一页速查：先看「角色口径」，再按模块表查接口。

## 1. 角色口径

| 角色 | 判定方式 | 说明 |
|---|---|---|
| 匿名 | 无有效 `Authorization: Bearer` | 仅登录、注册、刷新、品牌只读、`/api/media/auth/` 可达 |
| 登录用户 | `IsAuthenticated` 通过（JWT 有效、账号 `is_active`） | 全局默认权限，未显式声明的接口都至少是这一档 |
| 管理员 | DRF `IsAdminUser` → 仅看 `user.is_staff` | **不含**仅 `is_superuser=True` 但 `is_staff=False` 的账号 |
| 最后管理员口径 | 业务规则：`is_active=True` 且（`is_staff` 或 `is_superuser`） | 用于禁止降级/停用/删除最后一个管理账号 |

全局默认配置（`REST_FRAMEWORK`）：

- 认证：仅 `JWTAuthentication`（不支持 Session / Basic）。
- 权限：`IsAuthenticated`（新增 ViewSet 不写权限类时默认受保护）。
- 分页：`StandardPagination`，`PAGE_SIZE=20`，支持 `page_size`（上限 100）。

> 结论先行：**匿名可达的只有 5 个入口** —— `POST /api/auth/login/`、`POST /api/auth/register/`（默认 403）、`POST /api/auth/refresh/`、`GET /api/system/branding/`、`/api/media/auth/*`。
> 其余全部至少要求登录。

## 2. 受保护接口 × 角色

图例：`Y` 允许；`N` 拒绝（401 或 403）；`条件` 见备注。

### 2.1 认证与账号 `/api/auth/`

| 接口 | 匿名 | 登录用户 | 管理员 | 备注 |
|---|---|---|---|---|
| `POST /login/` | Y | Y | Y | AllowAny；失败节流 429 |
| `POST /register/` | Y | Y | Y | `ALLOW_REGISTER=False`（默认）时返回 403 |
| `POST /refresh/` | Y | Y | Y | AllowAny，标准轮换 + 拉黑 |
| `POST /logout/` | N | Y | Y | 幂等 |
| `GET/PUT/PATCH /profile/` | N | Y | Y | 仅本人；`department` 只读 |
| `POST /change-password/` | N | Y | Y | 仅本人；成功后吊销其它设备 refresh |
| `/manage/`、`/manage/{id}/` | N | N | Y | 用户增删改查、启停；PATCH 带 `password` 即重置密码 |
| `/manage/{id}/sessions/` | N | N | Y | 查看他人活跃登录 |
| `/manage/{id}/revoke-sessions/` | N | N | Y | 强制下线 |
| `/departments/`、`/departments/{id}/` | N | N | Y | 部门管理 |
| `/audit-logs/`、`/audit-logs/{id}/body/` | N | N | Y | 查看工具全文也会留痕 |

### 2.2 对话 `/api/chat/`

| 接口 | 匿名 | 登录用户 | 管理员 | 备注 |
|---|---|---|---|---|
| `/conversations/` 及其子路径 | N | Y | Y | 数据按 `user=本人` 过滤，**管理员也读不到他人会话** |
| `POST /chat/upload_attachment/` | N | Y | Y | 10 MB 上限、扩展名白名单 |
| `POST /chat/send/` | N | Y | Y | 会话归属本人；挂载资源需可见 |
| `POST /chat/trial/` | N | 条件 | 条件 | 仅目标智能体的创建者或管理员，否则 403 |
| `POST /chat/regenerate/` | N | Y | Y | 仅本人会话 |
| `POST /chat/permission/` | N | Y | Y | 只能处理自己会话里发起且未过期的请求 |
| `GET /models/health/`、`/models/config/` | N | Y | Y | 不返回密钥 |
| `GET /files/{id}/download/` | N | Y | Y | 仅文件属主 |

### 2.3 知识库 `/api/knowledge/`

| 接口 | 匿名 | 登录用户 | 管理员 | 备注 |
|---|---|---|---|---|
| `GET /bases/`、`/bases/{id}/` | N | 条件 | Y | 非管理员仅见「公开 + 本人 + 本部门」 |
| `POST/PUT/PATCH/DELETE /bases/` | N | 条件 | Y | 写需属主或管理员 |
| `GET /bases/{id}/stats/` | N | 条件 | Y | 可见即可读 |
| `GET /documents/`、`/documents/{id}/` | N | 条件 | Y | 跟随所属知识库可见性 |
| `POST /documents/`、`/upload/`、`/delete/`、`/reprocess/` | N | 条件 | Y | 需该库 `can_manage_kb` |
| `PATCH /documents/{id}/chunks/{chunk_id}/` | N | 条件 | Y | 需该库 `can_manage_kb` |
| `GET /documents/{id}/chunks/` | N | 条件 | Y | 可见即可读 |
| `POST /chat/ask/` | N | 条件 | Y | 知识库不可见按 404 处理 |

### 2.4 智能体 `/api/agents/`

| 接口 | 匿名 | 登录用户 | 管理员 | 备注 |
|---|---|---|---|---|
| `GET /`、`/options/`、`/summary/` | N | Y | Y | 列表按可见范围过滤 |
| `POST /`、`POST /{id}/clone/` | N | Y | Y | 任何登录用户都可新建草稿 |
| `GET /{id}/`、版本列表 / 对比 / `mount-options/` | N | 条件 | Y | 不可见返回 404 |
| `PUT/PATCH/DELETE /{id}/` | N | 条件 | Y | 创建者且非内置，或管理员 |
| `publish-check`、`publish`、`disable`、`enable`、`archive`、`restore`、`rollback` | N | 条件 | Y | 同上：`can_edit` |
| `restore-builtin` | N | 条件 | Y | 仅内置件的可编辑者（实际为管理员） |

> 智能体全部接口权限类都是 `IsAuthenticated`；**真正的门在对象级 `can_edit` / `can_see`**，不是 `IsAdminUser`。

### 2.5 MCP `/api/mcp/`

| 接口 | 匿名 | 登录用户 | 管理员 | 备注 |
|---|---|---|---|---|
| `GET /servers/`、`GET /servers/{id}/` | N | Y | Y | 非管理员按可见范围过滤 |
| `GET /servers/{id}/tools/`、`/references/` | N | Y | Y | 只读元数据 |
| `GET /servers/mountable/` | N | Y | Y | 装配页选择器 |
| `POST/PUT/PATCH/DELETE /servers/` | N | N | Y | 来源增删改 |
| `POST /servers/{id}/test/` | N | N | Y | 探测并发现工具 |
| `POST /servers/{id}/tools/toggle/` | N | N | Y | 工具启停 |
| `POST /servers/{id}/disable/`、`/enable/` | N | N | Y | 启停来源 |
| `GET /servers/command-prefixes/` | N | N | Y | 虽是 GET，仍属写集合 |
| `GET/POST /servers/egress/` | N | N | Y | 出网总开关，读也需管理员 |

### 2.6 技能 `/api/skills/`

| 接口 | 匿名 | 登录用户 | 管理员 | 备注 |
|---|---|---|---|---|
| `GET /`、`GET /{id}/`、`/versions/`、`/references/`、`/options/`、`/summary/` | N | Y | Y | 列表按可见范围过滤 |
| `GET /{id}/files/`、两个附件下载接口 | N | Y | Y | 只读 |
| `POST /`、`PATCH/DELETE /{id}/` | N | N | Y | 建档与元数据修改 |
| `POST /{id}/files/`、`DELETE /{id}/files/{fid}/` | N | N | Y | 附件增删，高危走 422 确认 |
| `approve`、`reject`、`disable`、`enable` | N | N | Y | 审核与启停 |
| `rollback` | N | N | Y | 版本回滚 |
| `GET/POST /mirror/` | N | N | Y | 运行时镜像 |

### 2.7 系统设置与用量

| 接口 | 匿名 | 登录用户 | 管理员 | 备注 |
|---|---|---|---|---|
| `GET /api/system/branding/` | Y | Y | Y | 登录页需要，允许匿名 |
| `POST /api/system/branding/` | N | N | Y | 改品牌 |
| `GET /api/system/settings/llm/` | N | Y | Y | 密钥掩码 |
| `POST /api/system/settings/llm/update/`、`/reset/` | N | N | Y | 改模型凭据 |
| `POST /api/system/settings/llm/test/` | N | **Y** | Y | **仅登录即可触发探测**，见风险说明 |
| `GET /api/system/settings/skill-package/` | N | Y | Y | 只读 |
| `POST /api/system/settings/skill-package/update/` | N | N | Y | 改附件类型白名单 |
| `GET /api/usage/overview/` | N | 条件 | Y | `scope=all` 仅管理员；`scope=dept` 限本部门 |

### 2.8 非 DRF 入口

| 入口 | 匿名 | 登录用户 | 管理员 | 判定方式 |
|---|---|---|---|---|
| `GET /api/media/auth/` | 401 | 条件 | 条件 | 不看 JWT，只看 HttpOnly Cookie `media_token`；无权 403 |

## 3. 资源级可见范围（public / department / private）

| 模块 | 是否支持三档 | 字段 | 部门匹配口径 |
|---|---|---|---|
| 知识库 | 是 | `scope`（权威）；`is_public` 仅兼容旧值 | `scope=department` 时比对 `department` 名称与用户部门 |
| 文档 / 分块 | 跟随知识库 | 无独立字段 | 同上 |
| 智能体 | 是 | `visibility`、`allowed_department_ids`、`include_sub_departments` | 用户部门 ∈ 授权部门；`include_sub_departments` 时包含下级；`admin_only` 仅管理员可见 |
| MCP 来源 | 是 | `visibility`、`allowed_department_ids` | 用户部门命中授权部门**或其上级**即可见 |
| 技能 | 是 | `visibility`、`allowed_department_ids` | 全企业 / 命中部门（含上级授权）/ 私有；另有 `scope_mode` + `applicable_department_ids` 控制**运行期适用范围** |
| 会话 / 消息 / 生成文件 | 否 | — | 严格按 `user` / 属主隔离，管理员不越权读取 |
| 用户 / 部门 / 审计 | 否 | — | 仅管理员 |

补充规则：

1. 可见性过滤在 `get_queryset` 完成；不可见对象的详情返回 404，避免泄露存在性。
2. 智能体草稿（`status=draft`）只有创建者与管理员可见；已发布版本沿用发布时的可见范围。
3. 技能列表**不按 `status` 过滤**，非管理员能看到停用/待审技能的元数据；能否被装配使用由 `mountable` / 运行期 `scope_mode` 决定。
4. 会话级工具授权存在 `Conversation.tool_grants`，只影响本会话，不改变上述可见范围，也不能借授权接口扩大调用目标。

## 4. 需要关注的事实与风险

1. `IsAdminUser` 只认 `is_staff`。只授予 `is_superuser` 而不给 `is_staff` 的账号调管理接口会 403，但「最后管理员保护」又把它算作管理员，两者口径不一致。
2. `POST /api/system/settings/llm/test/` 是唯一「登录即可写状态」的设置类接口：任意登录用户都能触发对已保存配置的模型探测，成功后会把模型名写入 `verified_models`。当前代码如此，是否收紧需产品确认。
3. `/api/mcp/servers/command-prefixes/` 和 `/egress/` 的 GET 也在 `WRITE_ACTIONS` 里，前端若要展示需按管理员菜单处理。
4. `/api/media/auth/` 在 Django 层没有 DRF 权限类，安全性完全依赖 Cookie 与路径匹配规则，改动该文件需同步 nginx 配置。
5. 管理员重置密码、强制下线、停用账号是三个独立动作：
   - 重置密码：吊销全部 refresh，并写 `password_reset` 审计；已签发的 access 最长还能用到自身过期（60 分钟）。
   - 强制下线：只吊销 refresh，账号状态不变，可立即重新登录；access 同样有最长 60 分钟残余窗口。
   - 停用账号：吊销 refresh，且 JWT 认证规则会拒绝 `is_active=False` 的账号，既有 access 立即失效。

---

关联文档：[API 接口文档](api-reference.md) · [备份与恢复记录模板](backup-restore-template.md) · [部署与回滚 Runbook](deploy-runbook.md)
