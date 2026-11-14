import { useState, type ReactNode } from 'react'
import { Badge } from '@/components/ui/Badge'
import { Button, IconButton } from '@/components/ui/Button'
import { CodeBlock } from '@/components/ui/CodeBlock'
import { EmptyState } from '@/components/ui/EmptyState'
import { Icon } from '@/components/ui/Icon'
import { Input } from '@/components/ui/Field'
import { Drawer } from '@/components/ui/Overlay'
import { Panel, SectionLabel } from '@/components/ui/Panel'
import { Seg } from '@/components/ui/Seg'
import { Switch } from '@/components/ui/Switch'
import { useMcpReferences, useMcpServer, useMcpTools, useSetMcpActive, useTestMcpServer, useToggleMcpTools } from '@/api/hooks/useMcp'
import { toast } from '@/store/toast'
import { useUserStore } from '@/store/user'
import { fmtDateTime, fmtRelative } from '@/utils/format'
import type { ConnectivityTest, McpServer } from '@/types/mcp'
import { BuiltinBadge, EgressBadge, StatusBadge } from './shared'

interface Props {
  serverId: number
  /** 列表行数据：详情请求返回前先用于渲染 */
  fallback: McpServer | null
  onClose: () => void
  onEdit: (server: McpServer) => void
  onDelete: (server: McpServer) => void
}

function Meta({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="list-row">
      <span className="hk w-[92px] flex-none">{k}</span>
      <span className="min-w-0 flex-1 text-xs text-sidebar-foreground">{children}</span>
    </div>
  )
}

/** B3/B4/B6 明细抽屉：连通测试、工具启停、引用检查、启停与删除入口 */
export function ServerDrawer({ serverId, fallback, onClose, onEdit, onDelete }: Props) {
  const isAdmin = useUserStore((s) => s.user?.is_staff ?? false)
  const { data: fresh } = useMcpServer(serverId)
  const server = fresh ?? fallback
  const [testResult, setTestResult] = useState<ConnectivityTest | null>(null)
  const [refsOpen, setRefsOpen] = useState(false)

  const test = useTestMcpServer()
  const active = useSetMcpActive()
  const { data: refs } = useMcpReferences(serverId, refsOpen)

  async function runTest() {
    try {
      const res = await test.mutateAsync(serverId)
      setTestResult(res)
      if (res.ok) toast.success(`连通正常：${res.tools.length} 个工具（新 ${res.new_tools.length} / 失效 ${res.missing_tools.length}）`)
      else toast.warning(`${res.failure_label || '连接失败'}：${res.failure_message}`)
    } catch (err) {
      toast.error((err as Error).message || '测试请求失败')
    }
  }

  if (!server) {
    return (
      <Drawer open onClose={onClose} title="来源明细" meta="读取中…">
        <div className="empty-state">
          <Icon name="LoaderCircle" size={18} className="animate-spin" />
        </div>
      </Drawer>
    )
  }

  return (
    <Drawer
      open
      onClose={onClose}
      title={
        <span className="flex items-center gap-[8px]">
          {server.name}
          <StatusBadge server={server} />
          {server.is_builtin && <BuiltinBadge />}
          {server.requires_egress && <EgressBadge />}
        </span>
      }
      meta={`标识 ${server.slug} · 工具全称 mcp__${server.slug}__{tool} · ${server.created_by_name || '未知创建人'} 创建于 ${fmtDateTime(server.created_at)}`}
      footer={
        isAdmin ? (
          <>
            <Button variant="ghost" icon="Pencil" onClick={() => onEdit(server)}>
              编辑
            </Button>
            <span className="flex-1" />
            <Button
              variant="ghost"
              icon={server.is_active ? 'PowerOff' : 'Power'}
              loading={active.isPending}
              onClick={async () => {
                try {
                  await active.mutateAsync({ id: server.id, active: !server.is_active })
                  toast.success(server.is_active ? '已停用：装配页即时不可挂载' : '已恢复：下次测试刷新状态')
                } catch (err) {
                  toast.error((err as Error).message)
                }
              }}
            >
              {server.is_active ? '停用' : '恢复'}
            </Button>
            {!server.is_builtin && (
              <Button variant="danger" icon="Trash2" onClick={() => onDelete(server)}>
                删除
              </Button>
            )}
          </>
        ) : (
          <span className="field-hint">只读视图：工具启停与来源配置仅创建人和平台管理员可操作</span>
        )
      }
    >
      <div className="col-stack">
        {server.last_error && (
          <div className="rounded-md border border-state-error-border bg-state-error-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-error">
            最近异常：{server.last_error}
          </div>
        )}

        {/* 连通性测试（B3） */}
        <Panel
          title="连通性"
          icon="Activity"
          meta={`最近检测 ${fmtRelative(server.last_check_at)}`}
          actions={
            isAdmin ? (
              <Button quiet variant="primary" icon="Zap" loading={test.isPending} onClick={runTest}>
                发起测试
              </Button>
            ) : undefined
          }
          padded
        >
          {testResult ? (
            <TestResultCard result={testResult} />
          ) : (
            <div className="field-hint">
              点击「发起测试」将按连接类型探活并刷新工具清单；新发现工具默认关闭，人工确认后启用。
            </div>
          )}
        </Panel>

        {/* 连接信息 */}
        <Panel title="连接信息" icon="Plug" padded>
          <div className="col-stack gap-[2px]">
            <Meta k="类型">{server.transport_display}</Meta>
            {server.transport === 'http_mcp' ? (
              <Meta k="地址">
                <span className="mono break-all">{server.url || '—'}</span>
              </Meta>
            ) : (
              <>
                <Meta k="命令行">
                  <span className="mono break-all">{[server.command, ...server.args].join(' ') || '—'}</span>
                </Meta>
                <Meta k="工作目录">
                  {server.cwd ? (
                    <span className="mono break-all">{server.cwd}</span>
                  ) : (
                    <span className="text-muted-foreground">（平台默认目录）</span>
                  )}
                </Meta>
              </>
            )}
            <Meta k="连接方式">
              {server.lifecycle_display} · {server.is_stateful ? '有状态' : '无状态'}
            </Meta>
            <Meta k="超时">
              <span className="tnum">{server.effective_timeout}s</span>
              {server.timeout_seconds == null && <span className="text-muted-foreground">（平台固定值：默认 {server.timeout_default}s）</span>}
            </Meta>
            {Object.keys(server.headers ?? {}).length > 0 && (
              <Meta k="请求头">
                <div className="flex flex-wrap gap-[4px]">
                  {Object.entries(server.headers).map(([k, v]) => (
                    <span key={k} className="chip mono">
                      {k}: {v}
                    </span>
                  ))}
                </div>
              </Meta>
            )}
            <Meta k="密钥">
              {server.has_secrets ? (
                <div className="flex flex-col gap-[4px]">
                  {(['headers', 'env'] as const).map((group) => {
                    const kv = server.secrets_masked?.[group] ?? {}
                    return Object.keys(kv).length > 0 ? (
                      <div key={group} className="flex flex-wrap gap-[4px]">
                        <span className="mono text-xs text-muted-foreground">{group}:</span>
                        {Object.entries(kv).map(([k, v]) => (
                          <span key={k} className="chip mono">
                            {k}={v}
                          </span>
                        ))}
                      </div>
                    ) : null
                  })}
                  <span className="text-xs text-muted-foreground">掩码后 4 位可见，原值永不回显</span>
                </div>
              ) : (
                '未配置'
              )}
            </Meta>
            <Meta k="可见范围">
              {server.visibility === 'department'
                ? `指定部门：${server.allowed_departments.join('、') || '—'}`
                : server.visibility === 'public'
                  ? '全企业'
                  : server.visibility === 'admin_only'
                    ? '仅管理端'
                    : '仅自己'}
            </Meta>
          </div>
        </Panel>

        {/* 工具清单（B4） */}
        <ToolsPanel server={server} canWrite={isAdmin} />

        {/* 引用检查（B6） */}
        <Panel
          title="引用情况"
          icon="Bot"
          meta={`装配中的智能体 ${server.agent_count} 个`}
          actions={
            <Button quiet variant="ghost" onClick={() => setRefsOpen((v) => !v)}>
              {refsOpen ? '收起清单' : '检查引用'}
            </Button>
          }
          padded
        >
          {!refsOpen ? (
            <div className="field-hint">被装配中的智能体引用时，停用不影响运行态，删除需先摘除挂载。</div>
          ) : !refs ? (
            <div className="text-xs text-muted-foreground">读取引用…</div>
          ) : refs.references.length === 0 ? (
            <div className="text-xs text-muted-foreground">无智能体引用，可直接删除</div>
          ) : (
            <div className="col-stack gap-[4px]">
              {refs.references.map((r) => (
                <div key={r.id} className="list-row">
                  <span className="row-main min-w-0">
                    <span className="row-title">{r.name}</span>
                    <span className="row-sub mono truncate">{r.tool_names.join('、')}</span>
                  </span>
                  <Badge tone={r.status === 'published' ? 'success' : 'neutral'}>{r.status}</Badge>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </Drawer>
  )
}

function TestResultCard({ result }: { result: ConnectivityTest }) {
  return (
    <div className="col-stack gap-[8px]">
      <div className="flex flex-wrap items-center gap-[8px]">
        <Badge tone={result.ok ? 'success' : 'error'} dot>
          {result.ok ? '连通正常' : result.failure_label || '连通失败'}
        </Badge>
        <span className="text-xs text-muted-foreground">
          耗时 <span className="tnum text-sidebar-foreground">{result.elapsed_ms}</span> ms · 超时设定{' '}
          <span className="tnum text-sidebar-foreground">{result.timeout_seconds}</span> s
        </span>
        {result.tools_stale && <Badge tone="warning">工具清单待刷新</Badge>}
      </div>
      {result.ok ? (
        <div className="field-hint">
          发现工具 <span className="tnum">{result.tools.length}</span> 个，其中新工具{' '}
          <span className="tnum text-state-warning">{result.new_tools.length}</span> 个（默认关闭，请人工确认启用）、失效工具{' '}
          <span className="tnum">{result.missing_tools.length}</span> 个。
        </div>
      ) : (
        <>
          {result.failure_message && (
            <div className="text-xs leading-relaxed text-state-error">{result.failure_message}</div>
          )}
          {result.detail && (
            <details>
              <summary className="cursor-pointer text-xs text-muted-foreground">查看原始错误</summary>
              <CodeBlock code={result.detail} maxHeight={180} className="mt-[6px]" />
            </details>
          )}
        </>
      )}
      {!result.managed_runtime_ready && result.managed_runtime_note && (
        <div className="field-hint">运行态提示：{result.managed_runtime_note}</div>
      )}
    </div>
  )
}

/** 工具清单：搜索 + 状态过滤 + 分页 + 单工具启停（新工具默认关闭口径不变） */
function ToolsPanel({ server, canWrite }: { server: McpServer; canWrite: boolean }) {
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [state, setState] = useState('')
  const toggle = useToggleMcpTools(server.id)

  const { data, isFetching } = useMcpTools(server.id, {
    page,
    page_size: 20,
    ...(search ? { search } : {}),
    ...(state ? { state } : {}),
  })

  function commitSearch(v: string) {
    setSearch(v.trim())
    setPage(1)
  }

  return (
    <Panel
      title="工具清单"
      icon="Wrench"
      meta={
        data
          ? `启用 ${server.enabled_tool_count}/${server.tool_count} · 新 ${server.new_tool_count}`
          : `启用 ${server.enabled_tool_count}/${server.tool_count}`
      }
      padded={false}
    >
      <div className="page-toolbar rounded-none border-x-0">
        <div className="relative w-[240px]">
          <Icon name="Search" size={13} className="pointer-events-none absolute left-[9px] top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input
            className="pl-[28px]"
            value={searchInput}
            placeholder="搜索工具名…"
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && commitSearch(searchInput)}
            onBlur={() => commitSearch(searchInput)}
          />
        </div>
        <Seg
          items={[
            { value: '', label: '全部' },
            { value: 'present', label: '有效' },
            { value: 'missing', label: '已失效' },
          ]}
          value={state}
          onChange={(v) => {
            setState(v)
            setPage(1)
          }}
        />
        {isFetching && <Icon name="LoaderCircle" size={13} className="animate-spin text-muted-foreground" />}
      </div>

      {!data || data.results.length === 0 ? (
        <EmptyState
          compact
          icon="Wrench"
          title={server.tool_count === 0 ? '尚未发现工具' : '无匹配工具'}
          description={
            server.tool_count === 0
              ? '先执行连通性测试，平台会自动发现并落库工具（新工具默认关闭）'
              : '调整搜索或状态过滤后重试'
          }
        />
      ) : (
        <div>
          {data.results.map((tool) => (
            <div key={tool.name} className="list-row">
              <span className="row-main min-w-0">
                <span className="row-title flex items-center gap-[6px] truncate">
                  <span className="mono">{tool.name}</span>
                  {tool.state === 'missing' && <Badge tone="warning">已失效</Badge>}
                </span>
                <span className="row-sub line-clamp-1">
                  {tool.description || '（无描述）'}
                  {tool.params.length > 0 && (
                    <span className="text-muted-foreground">
                      {' '}·{' '}
                      <span className="mono">
                        {tool.params
                          .slice(0, 5)
                          .map((p) => `${p.name}${p.required ? '*' : ''}:${p.type}`)
                          .join(' ')}
                      </span>
                      {tool.params.length > 5 && (
                        <span>
                          {' '}…共 <span className="tnum">{tool.params.length}</span> 项
                        </span>
                      )}
                    </span>
                  )}
                </span>
              </span>
              <Switch
                checked={tool.enabled}
                disabled={!canWrite || toggle.isPending}
                label={`启停 ${tool.name}`}
                onChange={async (next) => {
                  try {
                    await toggle.mutateAsync({ names: [tool.name], enabled: next })
                    toast.success(next ? `已启用工具 ${tool.name}` : `已停用工具 ${tool.name}`)
                  } catch (err) {
                    toast.error((err as Error).message)
                  }
                }}
              />
            </div>
          ))}
        </div>
      )}

      {data && data.count > 20 && (
        <div className="panel-foot justify-end">
          <IconButton icon="ChevronLeft" label="上一页" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))} />
          <span className="tnum text-xs text-muted-foreground">
            {page}/{Math.max(Math.ceil(data.count / 20), 1)}
          </span>
          <IconButton
            icon="ChevronRight"
            label="下一页"
            disabled={page >= Math.ceil(data.count / 20)}
            onClick={() => setPage((p) => p + 1)}
          />
        </div>
      )}
      {!canWrite && server.tool_count > 0 && (
        <div className="px-[12px] pb-[8px] text-xs text-muted-foreground">
          <SectionLabel className="inline">只读</SectionLabel> 工具启停仅创建人和平台管理员可操作
        </div>
      )}
    </Panel>
  )
}
