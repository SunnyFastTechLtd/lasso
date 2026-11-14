import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { PageContainer, PageHeader } from '@/components/AppShell'
import { Badge } from '@/components/ui/Badge'
import { Button, IconButton } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/EmptyState'
import { Input } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { DataTable } from '@/components/ui/DataTable'
import { Pagination } from '@/components/ui/Pagination'
import { Panel, SectionLabel } from '@/components/ui/Panel'
import { ConfirmDialog, Modal } from '@/components/ui/Overlay'
import { Seg } from '@/components/ui/Seg'
import { Switch } from '@/components/ui/Switch'
import {
  useDeleteMcpServer,
  useEgress,
  useImportMcpServers,
  useMcpServers,
  useMcpStatusCounts,
  useSetEgress,
  useSetMcpActive,
  useTestMcpServer,
} from '@/api/hooks/useMcp'
import { useUserStore } from '@/store/user'
import { toast } from '@/store/toast'
import { ApiError } from '@/api/request'
import { mcpApi } from '@/api/mcp'
import { fmtRelative } from '@/utils/format'
import type { DeleteConflictBody, McpListParams, McpServer } from '@/types/mcp'
import { BuiltinBadge, EgressBadge, StatusBadge } from './shared'
import { ServerDrawer } from './ServerDrawer'
import { ServerForm } from './ServerForm'
import { MarketBrowser } from '@/pages/Market'

const STATUS_SEG: { value: string; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'healthy', label: '正常' },
  { value: 'unknown', label: '未验证' },
  { value: 'error', label: '异常' },
  { value: 'disabled', label: '已停用' },
  { value: 'egress_blocked', label: '出网未开放' },
]

export default function McpPage() {
  const isAdmin = useUserStore((s) => s.user?.is_staff ?? false)
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = searchParams.get('tab') === 'market' ? 'market' : 'local'

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [status, setStatus] = useState('')
  const [transport, setTransport] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')

  const [drawerId, setDrawerId] = useState<number | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [formServer, setFormServer] = useState<McpServer | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<McpServer | null>(null)
  const [conflict, setConflict] = useState<{ server: McpServer; body: DeleteConflictBody } | null>(null)

  useEffect(() => {
    const t = window.setTimeout(() => {
      setSearch(searchInput.trim())
      setPage(1)
    }, 350)
    return () => window.clearTimeout(t)
  }, [searchInput])

  const params: McpListParams = {
    page,
    page_size: pageSize,
    ...(search ? { search } : {}),
    ...(status ? { status } : {}),
    ...(transport ? { transport } : {}),
  }

  const { data, isFetching } = useMcpServers(params)
  const { data: counts } = useMcpStatusCounts()
  const { data: egress } = useEgress(isAdmin)
  const setEgress = useSetEgress()
  const quickTest = useTestMcpServer()
  const setActive = useSetMcpActive()
  const del = useDeleteMcpServer()
  const importServers = useImportMcpServers()
  const fileRef = useRef<HTMLInputElement>(null)

  async function exportConfig() {
    try {
      const data = await mcpApi.exportConfig()
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `mcp-servers-${new Date().toISOString().slice(0, 10)}.json`
      link.click()
      URL.revokeObjectURL(url)
      toast.success(`已导出 ${data.count} 个来源，密钥需在目标环境重新填写`)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  async function importFile(file: File) {
    try {
      const payload = JSON.parse(await file.text())
      if (!payload || !Array.isArray(payload.servers)) throw new Error('文件格式不正确：缺少 servers 数组')
      const res = await importServers.mutateAsync({ version: payload.version ?? 1, servers: payload.servers })
      const parts = [`新建 ${res.created.length} 个`, `跳过 ${res.skipped.length} 个`]
      if (res.errors.length) parts.push(`失败 ${res.errors.length} 个`)
      if (res.errors.length) toast.warning(`${parts.join('，')}：${res.errors[0].detail}`)
      else toast.success(parts.join('，'))
    } catch (err) {
      toast.error((err as Error).message)
    } finally {
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  function openDrawer(server: McpServer) {
    setDrawerId(server.id)
  }

  function openCreate() {
    setFormServer(null)
    setFormOpen(true)
  }

  function openEdit(server: McpServer) {
    setFormServer(server)
    setFormOpen(true)
  }

  async function runQuickTest(server: McpServer) {
    try {
      const res = await quickTest.mutateAsync(server.id)
      if (res.ok) toast.success(`${server.name}：连通正常，${res.tools.length} 个工具 / ${res.elapsed_ms}ms`)
      else toast.warning(`${server.name}：${res.failure_label || '连接失败'}——${res.failure_message}`)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  async function toggleActive(server: McpServer) {
    try {
      await setActive.mutateAsync({ id: server.id, active: !server.is_active })
      toast.success(server.is_active ? `已停用「${server.name}」` : `已恢复「${server.name}」`)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return
    try {
      await del.mutateAsync({ id: deleteTarget.id })
      toast.success(`已删除「${deleteTarget.name}」`)
      setDeleteTarget(null)
      if (drawerId === deleteTarget.id) setDrawerId(null)
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const body = err.body as Partial<DeleteConflictBody> | null
        if (body && body.code === 'mcp_server_referenced') {
          setConflict({ server: deleteTarget, body: body as DeleteConflictBody })
          setDeleteTarget(null)
        } else {
          toast.error(err.message)
        }
      } else {
        toast.error((err as Error).message)
      }
    }
  }

  async function conflictDisableOnly() {
    if (!conflict) return
    try {
      await setActive.mutateAsync({ id: conflict.server.id, active: false })
      toast.success(`已停用「${conflict.server.name}」，智能体挂载保持不变`)
      setConflict(null)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  async function conflictForceDelete() {
    if (!conflict) return
    try {
      await del.mutateAsync({ id: conflict.server.id, confirmUnmount: true })
      toast.success(`已删除「${conflict.server.name}」并从 ${conflict.body.references.length} 个智能体摘除`)
      setConflict(null)
      if (drawerId === conflict.server.id) setDrawerId(null)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  if (tab === 'market') {
    return (
      <PageContainer>
        <PageHeader
          title="工具"
          subtitle="公开 MCP 目录检索：导入走单级审批，落库为「未验证」且工具默认关闭"
          actions={
            <Seg
              items={[
                { value: 'local', label: '工具来源' },
                { value: 'market', label: '市场检索' },
              ]}
              value={tab}
              onChange={(next) => setSearchParams(next === 'market' ? { tab: 'market' } : {})}
            />
          }
        />
        <div className="mt-[14px]">
          <MarketBrowser kind="mcp" embedded />
        </div>
      </PageContainer>
    )
  }

  return (
    <PageContainer>
      <PageHeader
        title="工具"
        subtitle="接入企业已有 MCP 服务或内部命令行工具；也可在市场里检索公开来源，导入需管理员审批"
        actions={
          <div className="flex items-center gap-[8px]">
            <Seg
              items={[
                { value: 'local', label: '工具来源' },
                { value: 'market', label: '市场检索' },
              ]}
              value={tab}
              onChange={(next) => setSearchParams(next === 'market' ? { tab: 'market' } : {})}
            />
            {isAdmin ? (
              <div className="flex items-center gap-[8px]">
                <input
                  ref={fileRef}
                  type="file"
                  accept="application/json,.json"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0]
                    if (file) void importFile(file)
                  }}
                />
                <Button variant="ghost" icon="Upload" disabled={importServers.isPending} onClick={() => fileRef.current?.click()}>
                  导入
                </Button>
                <Button variant="ghost" icon="Download" onClick={() => void exportConfig()}>
                  导出
                </Button>
                <Button variant="primary" icon="Plus" onClick={openCreate}>
                  新增来源
                </Button>
              </div>
            ) : (
              <Badge tone="neutral">只读视图</Badge>
            )}
          </div>
        }
      />

      <div className="mt-[14px] grid grid-cols-2 gap-[10px] md:grid-cols-5">
        {(
          [
            ['总来源', counts?.total, ''],
            ['正常', counts?.healthy, 'success'],
            ['未验证', counts?.unknown, 'neutral'],
            ['异常', counts?.error, 'error'],
            ['出网未开放', counts?.egress_blocked, 'warning'],
          ] as const
        ).map(([label, value, tone]) => (
          <div key={label} className="panel px-[12px] py-[10px]">
            <SectionLabel>{label}</SectionLabel>
            <div className={`metric-value mt-[4px] ${tone === 'success' ? 'text-state-success' : tone === 'error' ? 'text-state-error' : tone === 'warning' ? 'text-state-warning' : ''}`}>
              {value ?? <span className="text-muted-foreground">—</span>}
            </div>
          </div>
        ))}
      </div>

      <div className="page-toolbar">
        <div className="relative w-[240px]">
          <Icon name="Search" size={13} className="pointer-events-none absolute left-[9px] top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input
            className="pl-[28px]"
            value={searchInput}
            placeholder="搜索名称 / 标识 / 地址…"
            onChange={(e) => setSearchInput(e.target.value)}
          />
        </div>
        <Seg
          items={STATUS_SEG}
          value={status}
          onChange={(v) => {
            setStatus(v)
            setPage(1)
          }}
        />
        <Seg
          items={[
            { value: '', label: '全部类型' },
            { value: 'http_mcp', label: '接口型' },
            { value: 'stdio', label: '命令型' },
          ]}
          value={transport}
          onChange={(v) => {
            setTransport(v)
            setPage(1)
          }}
        />
        <span className="toolbar-note">新发现工具默认关闭，需人工启用</span>
        <span className="flex-1" />
        {isAdmin && egress && (
          <div className="flex items-center gap-[8px]">
            <span className="text-xs text-muted-foreground">
              全平台出网
              {egress.blocked_count !== undefined && (
                <span className="tnum text-sidebar-foreground">（当前受限 {egress.blocked_count} 个）</span>
              )}
            </span>
            <Switch
              checked={egress.enabled}
              disabled={setEgress.isPending}
              label="出网开关"
              onChange={async (next) => {
                try {
                  const res = await setEgress.mutateAsync({ enabled: next })
                  toast.success(
                    res.enabled
                      ? '出网已放开：受限来源恢复可达，请逐一重新测试'
                      : '出网已关闭：出网类来源转入「出网未开放」',
                  )
                } catch (err) {
                  toast.error((err as Error).message)
                }
              }}
            />
            <span className="text-xs text-muted-foreground">严格</span>
            <Switch
              checked={Boolean(egress.strict_domains)}
              disabled={setEgress.isPending}
              label="严格域名白名单"
              onChange={async (next) => {
                try {
                  await setEgress.mutateAsync({ strict_domains: next })
                  toast.success(
                    next
                      ? '已开启严格白名单：出网来源只允许访问其声明域名，且解析结果不得落私网段'
                      : '已关闭严格白名单：恢复原有行为（只受全平台出网开关约束）',
                  )
                } catch (err) {
                  toast.error((err as Error).message)
                }
              }}
            />
          </div>
        )}
      </div>

      <Panel padded={false}>
        <DataTable
          className="data-table-fixed"
          columns={[
            {
              key: 'name',
              title: '名称',
              render: (s) => (
                <span className="row-main">
                  <span className="row-title flex items-center gap-[6px]">
                    {s.name}
                    {s.is_builtin && <BuiltinBadge />}
                    {s.requires_egress && <EgressBadge />}
                  </span>
                  <span className="row-sub truncate">
                    <span className="mono">{s.slug}</span>
                    {s.description ? ` · ${s.description}` : ''}
                  </span>
                </span>
              ),
            },
            { key: 'status', title: '状态', width: 110, render: (s) => <StatusBadge server={s} /> },
            {
              key: 'transport',
              title: '连接类型',
              width: 132,
              secondary: true,
              render: (s) => (
                <span className="flex flex-col leading-[1.35]">
                  <span className="text-sidebar-foreground">{s.transport_display}</span>
                  <span className="mono text-xs text-muted-foreground truncate max-w-[160px]">
                    {s.transport === 'http_mcp' ? s.url : s.command}
                  </span>
                </span>
              ),
            },
            {
              key: 'tools',
              title: '工具（启用/总）',
              tnum: true,
              align: 'center',
              width: 130,
              render: (s) => (
                <span className="inline-flex items-center gap-[4px]">
                  {s.enabled_tool_count}/{s.tool_count}
                  {s.new_tool_count > 0 && (
                    <Badge tone="warning" title="新发现工具默认关闭，请人工确认">
                      +{s.new_tool_count} 新
                    </Badge>
                  )}
                </span>
              ),
            },
            { key: 'agent_count', title: '引用', tnum: true, align: 'center', width: 64, secondary: true },
            {
              key: 'last_check_at',
              title: '最后检测',
              tnum: true,
              width: 110,
              secondary: true,
              render: (s) => <span className="text-xs">{fmtRelative(s.last_check_at)}</span>,
            },
            {
              key: 'ops',
              title: '操作',
              align: 'right',
              width: 128,
              render: (s) =>
                isAdmin ? (
                  <span className="flex items-center justify-end gap-[2px]" onClick={(e) => e.stopPropagation()}>
                    <IconButton icon="Zap" label="连通性测试" disabled={quickTest.isPending} onClick={() => runQuickTest(s)} />
                    <IconButton
                      icon={s.is_active ? 'PowerOff' : 'Power'}
                      label={s.is_active ? '停用' : '恢复'}
                      onClick={() => toggleActive(s)}
                    />
                    {!s.is_builtin && <IconButton icon="Trash2" label="删除" onClick={() => setDeleteTarget(s)} />}
                  </span>
                ) : (
                  <span className="text-xs text-muted-foreground">只读</span>
                ),
            },
          ]}
          rows={data?.results ?? []}
          rowKey={(s) => s.id}
          loading={isFetching && !data}
          onRowClick={openDrawer}
          empty={
            <EmptyState
              icon="Plug"
              title={search || status ? '没有符合条件的来源' : '还没有接入工具来源'}
              description={
                isAdmin
                  ? search || status
                    ? '换个筛选条件试试；测试失败或停用后状态会在这里更新'
                    : '新增一个 MCP 服务地址或命令行工具，保存后平台会自动测一次连通性'
                  : '当前账号看不到已接入的来源，请联系平台管理员配置可见范围'
              }
              action={
                !search && !status && isAdmin ? (
                  <Button variant="primary" icon="Plus" onClick={openCreate}>
                    新增来源
                  </Button>
                ) : undefined
              }
            />
          }
        />
        {data && data.count > 0 && (
          <Pagination
            className="rounded-b-lg"
            page={page}
            totalPages={Math.ceil(data.count / pageSize)}
            count={data.count}
            pageSize={pageSize}
            onPageChange={setPage}
            onPageSizeChange={(v) => {
              setPageSize(v)
              setPage(1)
            }}
          />
        )}
      </Panel>

      {drawerId !== null && (
        <ServerDrawer
          serverId={drawerId}
          fallback={(data?.results ?? []).find((s) => s.id === drawerId) ?? null}
          onClose={() => setDrawerId(null)}
          onEdit={(s) => {
            setDrawerId(null)
            openEdit(s)
          }}
          onDelete={(s) => {
            setDrawerId(null)
            setDeleteTarget(s)
          }}
        />
      )}

      <ServerForm open={formOpen} server={formServer} onClose={() => setFormOpen(false)} />

      <ConfirmDialog
        open={deleteTarget !== null}
        title={`删除来源「${deleteTarget?.name ?? ''}」`}
        tone="danger"
        confirmText="尝试删除"
        message={
          <>
            平台会先做引用检查：若仍有智能体挂载其工具，将给出「停用保留」或「删除并摘除」二选一；内置演示来源不允许删除。
            {deleteTarget?.status === 'unknown' && (
              <div className="mt-[6px] text-state-warning">该来源尚未验证连通，删除后需重新建档并测试。</div>
            )}
          </>
        }
        loading={del.isPending}
        onConfirm={confirmDelete}
        onCancel={() => setDeleteTarget(null)}
      />

      <Modal
        open={conflict !== null}
        title="来源仍被引用"
        icon="TriangleAlert"
        onClose={() => setConflict(null)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConflict(null)} disabled={setActive.isPending || del.isPending}>
              取消
            </Button>
            <Button variant="default" icon="PowerOff" loading={setActive.isPending} onClick={conflictDisableOnly}>
              仅停用（保留挂载）
            </Button>
            <Button variant="danger" icon="Trash2" loading={del.isPending} onClick={conflictForceDelete}>
              删除并摘除挂载
            </Button>
          </>
        }
      >
        <div className="col-stack gap-[8px]">
          <p className="text-xs leading-[1.6] text-muted-foreground">
            {conflict?.body.detail ?? '以下智能体挂载了该来源的工具。停用不影响运行态；删除将自动从这些智能体摘除对应工具授权。'}
          </p>
          <div className="col-stack gap-[4px]">
            {(conflict?.body.references ?? []).map((r) => (
              <div key={r.id} className="list-row">
                <span className="row-main min-w-0">
                  <span className="row-title">{r.name}</span>
                  <span className="row-sub mono truncate">{r.tool_names.join('、')}</span>
                </span>
                <Badge tone={r.status === 'published' ? 'success' : 'neutral'}>{r.status}</Badge>
              </div>
            ))}
          </div>
          <div className="field-hint">
            二选一来自接口契约 options={JSON.stringify(conflict?.body.options ?? [])}；删除并摘除会写审计，操作不可自动回滚。
          </div>
        </div>
      </Modal>
    </PageContainer>
  )
}
