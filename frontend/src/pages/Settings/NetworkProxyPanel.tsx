import { useEffect, useMemo, useState } from 'react'
import { ApiError } from '@/api/request'
import {
  useApplyProxies, useCreateProxy, useDeleteProxy, useProxyState, useSaveBindings,
  useTestProxy, useUpdateProxy,
} from '@/api/proxies'
import type {
  EngineBinding, ProxyEndpoint, ProxyPayload, ProxyScheme, ProxyState, ProxyTestResult,
  TiangongBinding,
} from '@/api/proxies'
import { Badge } from '@/components/ui/Badge'
import { Button, IconButton } from '@/components/ui/Button'
import { DataTable } from '@/components/ui/DataTable'
import { EmptyState } from '@/components/ui/EmptyState'
import { Field, Input, Select } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { ConfirmDialog } from '@/components/ui/Overlay'
import { Panel, SectionLabel } from '@/components/ui/Panel'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/store/toast'

const EMPTY_FORM: ProxyPayload = {
  name: '',
  scheme: 'http',
  host: '',
  port: 7890,
  username: '',
  password: '',
  enabled: true,
  note: '',
}

const TEST_REASON: Record<string, string> = {
  timeout: '连接超时',
  refused: '连接被拒绝',
  auth_failed: '认证失败（407）',
  tls_error: 'TLS/证书错误',
  http_error: '目标返回错误',
  network_error: '网络错误',
  proxy_error: '代理不可达',
}

const STATUS_BADGE: Record<string, { tone: 'neutral' | 'success' | 'error'; label: string }> = {
  untested: { tone: 'neutral', label: '未测试' },
  ok: { tone: 'success', label: '通过' },
  failed: { tone: 'error', label: '失败' },
}

type LocalBinding = { kind: 'domain' | 'source'; key: string; proxy_id: number }

function bindingsOf(state: ProxyState | undefined) {
  const engines: Record<string, number | null> = {}
  state?.engines.forEach((row: EngineBinding) => {
    engines[row.name] = row.proxy_id
  })
  const tiangong: LocalBinding[] = (state?.tiangong ?? []).map((row: TiangongBinding) => ({
    kind: row.kind,
    key: row.key,
    proxy_id: row.proxy_id,
  }))
  return { engines, tiangong }
}

export function NetworkProxyPanel() {
  const query = useProxyState(true)
  const state = query.data
  const proxies = useMemo(() => state?.proxies ?? [], [state])
  const [local, setLocal] = useState<{ engines: Record<string, number | null>; tiangong: LocalBinding[] } | null>(null)

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<ProxyEndpoint | null>(null)
  const [form, setForm] = useState<ProxyPayload>(EMPTY_FORM)
  const [formError, setFormError] = useState('')
  const [testingId, setTestingId] = useState<number | null>(null)
  const [tests, setTests] = useState<Record<number, ProxyTestResult>>({})
  const [deleting, setDeleting] = useState<ProxyEndpoint | null>(null)
  const [newDomain, setNewDomain] = useState('')
  const [newDomainProxy, setNewDomainProxy] = useState<number | ''>('')
  const [bindingError, setBindingError] = useState('')

  const createProxy = useCreateProxy()
  const updateProxy = useUpdateProxy()
  const deleteProxy = useDeleteProxy()
  const testProxy = useTestProxy()
  const saveBindings = useSaveBindings()
  const applyBindings = useApplyProxies()

  // 首次拿到数据时灌入本地编辑态；之后由用户编辑驱动，避免轮询覆盖未保存改动
  useEffect(() => {
    if (state && local === null) setLocal(bindingsOf(state))
  }, [state, local])

  const enabledProxies = proxies.filter((p) => p.enabled)
  const httpProxies = enabledProxies.filter((p) => p.scheme === 'http')
  const applyState = state?.apply
  const applies = Boolean(local)
  const tiangongApplied = useMemo(() => {
    const map = new Map<string, boolean>()
    state?.tiangong.forEach((row: TiangongBinding) => {
      map.set(`${row.kind}:${row.key}`, row.applied)
    })
    return map
  }, [state])

  function openCreate() {
    setEditing(null)
    setForm(EMPTY_FORM)
    setFormError('')
    setFormOpen(true)
  }

  function openEdit(proxy: ProxyEndpoint) {
    setEditing(proxy)
    setForm({
      name: proxy.name,
      scheme: proxy.scheme,
      host: proxy.host,
      port: proxy.port,
      username: proxy.username,
      password: '',
      enabled: proxy.enabled,
      note: proxy.note,
    })
    setFormError('')
    setFormOpen(true)
  }

  async function submitForm() {
    setFormError('')
    if (!form.name.trim()) return setFormError('名称不能为空')
    if (!form.host.trim()) return setFormError('主机不能为空')
    if (!form.port) return setFormError('端口不能为空')
    const payload: ProxyPayload = { ...form, name: form.name.trim(), host: form.host.trim() }
    if (!payload.password) delete payload.password
    try {
      const res = editing
        ? await updateProxy.mutateAsync({ id: editing.id, payload })
        : await createProxy.mutateAsync(payload)
      setFormOpen(false)
      setEditing(null)
      if (res.apply?.applied === false) {
        toast.error(`代理已保存，但 SearXNG 应用失败：${res.apply.detail || res.apply.error}`)
      } else {
        toast.success('代理已保存并应用')
      }
    } catch (err) {
      setFormError((err as ApiError).message)
    }
  }

  async function runTest(proxy: ProxyEndpoint) {
    setTestingId(proxy.id)
    try {
      const res = await testProxy.mutateAsync(proxy.id)
      setTests((prev) => ({ ...prev, [proxy.id]: res }))
      if (res.ok) toast.success(`${proxy.name} 连通正常（${res.elapsed_ms}ms）`)
      else toast.error(`${proxy.name} 测试失败：${TEST_REASON[res.reason] || res.reason}`)
    } catch (err) {
      toast.error((err as ApiError).message)
    } finally {
      setTestingId(null)
    }
  }

  async function confirmDelete() {
    if (!deleting) return
    try {
      const res = await deleteProxy.mutateAsync(deleting.id)
      setLocal((prev) =>
        prev
          ? {
              engines: Object.fromEntries(
                Object.entries(prev.engines).map(([k, v]) => [k, v === deleting.id ? null : v]),
              ),
              tiangong: prev.tiangong.filter((row) => row.proxy_id !== deleting.id),
            }
          : prev,
      )
      if (res.apply?.applied === false) {
        toast.error(`代理已删除，但 SearXNG 应用失败：${res.apply.detail || res.apply.error}`)
      } else {
        toast.success('代理已删除')
      }
    } catch (err) {
      toast.error((err as ApiError).message)
    } finally {
      setDeleting(null)
    }
  }

  function addDomain() {
    setBindingError('')
    const key = newDomain.trim().toLowerCase().replace(/\.$/, '')
    if (!key) return setBindingError('填写域名，例如 example.com 或 *.example.com')
    if (!newDomainProxy) return setBindingError('先选择一个代理')
    if (!local) return
    if (local.tiangong.some((row) => row.kind === 'domain' && row.key === key)) {
      return setBindingError('该域名已绑定，直接改右侧下拉即可')
    }
    setLocal({
      ...local,
      tiangong: [...local.tiangong, { kind: 'domain', key, proxy_id: Number(newDomainProxy) }],
    })
    setNewDomain('')
  }

  function removeTiangong(index: number) {
    if (!local) return
    setLocal({ ...local, tiangong: local.tiangong.filter((_, i) => i !== index) })
  }

  async function submitBindings() {
    if (!local) return
    setBindingError('')
    try {
      const res = await saveBindings.mutateAsync({ engines: local.engines, tiangong: local.tiangong })
      setLocal(bindingsOf(res.state))
      if (res.apply?.applied) {
        toast.success('绑定已保存并写入 SearXNG')
      } else {
        toast.error(`绑定已保存，但应用失败：${res.apply?.detail || res.apply?.error || '未知原因'}`)
      }
    } catch (err) {
      setBindingError((err as ApiError).message)
    }
  }

  async function retryApply() {
    try {
      const res = await applyBindings.mutateAsync()
      if (res.apply?.applied) toast.success('已重新应用并通过健康检查')
      else toast.error(`应用失败：${res.apply?.detail || res.apply?.error || '未知原因'}`)
    } catch (err) {
      toast.error((err as ApiError).message)
    }
  }

  const columns = [
    {
      key: 'name',
      title: '名称',
      render: (row: ProxyEndpoint) => (
        <span className="flex min-w-0 flex-col gap-[1px]">
          <span className="row-title truncate">{row.name}</span>
          {row.note && <span className="row-sub truncate">{row.note}</span>}
        </span>
      ),
    },
    {
      key: 'address',
      title: '地址',
      mono: true,
      render: (row: ProxyEndpoint) => (
        <span className="flex min-w-0 items-center gap-[6px]">
          <span className="truncate">{row.display_url}</span>
          {row.username && <Badge tone="neutral">认证</Badge>}
        </span>
      ),
    },
    {
      key: 'scheme',
      title: '协议',
      width: 78,
      render: (row: ProxyEndpoint) => (
        <span className="text-xs text-muted-foreground">{row.scheme === 'socks5' ? 'SOCKS5' : 'HTTP'}</span>
      ),
    },
    {
      key: 'enabled',
      title: '启用',
      width: 64,
      render: (row: ProxyEndpoint) =>
        row.enabled ? <Badge tone="success" dot>启用</Badge> : <Badge tone="neutral">停用</Badge>,
    },
    {
      key: 'test',
      title: '最近测试',
      width: 220,
      render: (row: ProxyEndpoint) => {
        const result = tests[row.id]
        const status = result ? result.status : row.last_test_status
        const ms = result ? result.elapsed_ms : row.last_test_ms
        const error = result ? (result.ok ? '' : result.message) : row.last_test_error
        const badge = STATUS_BADGE[status] ?? STATUS_BADGE.untested
        return (
          <span className="flex min-w-0 flex-col gap-[2px]">
            <span className="flex items-center gap-[6px]">
              <Badge tone={badge.tone} dot={status !== 'untested'}>
                {badge.label}
              </Badge>
              {status === 'ok' && ms != null && (
                <span className="tnum text-xs text-muted-foreground">{ms}ms</span>
              )}
            </span>
            {error && (
              <details className="min-w-0">
                <summary className="cursor-pointer text-xs text-state-error">原始错误</summary>
                <div className="mono mt-[2px] max-h-[72px] overflow-auto whitespace-pre-wrap break-all text-xs text-muted-foreground">
                  {error}
                </div>
              </details>
            )}
          </span>
        )
      },
    },
    {
      key: 'actions',
      title: '操作',
      width: 150,
      align: 'right' as const,
      render: (row: ProxyEndpoint) => (
        <span className="flex items-center justify-end gap-[4px]">
          <Button quiet variant="ghost" loading={testingId === row.id} onClick={() => void runTest(row)}>
            测试
          </Button>
          <IconButton icon="Pencil" label="编辑" onClick={() => openEdit(row)} />
          <IconButton icon="Trash2" label="删除" onClick={() => setDeleting(row)} />
        </span>
      ),
    },
  ]

  return (
    <>
      {applyState?.status === 'failed' && (
        <div className="flex flex-wrap items-center gap-[8px] rounded-md border border-state-error bg-surface-sunken px-[10px] py-[8px]">
          <Badge tone="error" dot>未生效</Badge>
          <span className="min-w-0 flex-1 text-xs text-state-error">
            SearXNG 应用失败：{applyState.detail || applyState.error || '未知原因'}
            （绑定已保存，线上仍是上一份配置）
          </span>
          <Button quiet variant="ghost" loading={applyBindings.isPending} onClick={() => void retryApply()}>
            重试应用
          </Button>
        </div>
      )}

      <Panel
        title="代理条目"
        icon="Globe"
        meta={`${proxies.length} 个 · 启用 ${enabledProxies.length}`}
        padded
        actions={
          <Button variant="primary" quiet icon="Plus" onClick={openCreate}>
            新增代理
          </Button>
        }
      >
        {formOpen && (
          <div className="mb-[12px] rounded-md border border-border bg-surface-sunken px-[10px] py-[10px]">
            <div className="grid grid-cols-2 gap-[10px] md:grid-cols-4">
              <Field label="名称" required>
                <Input
                  value={form.name}
                  placeholder="如 内网出口"
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                />
              </Field>
              <Field label="协议">
                <Select
                  value={form.scheme}
                  onChange={(e) => setForm({ ...form, scheme: e.target.value as ProxyScheme })}
                >
                  <option value="http">HTTP</option>
                  <option value="socks5">SOCKS5（仅 SearXNG 引擎可用）</option>
                </Select>
              </Field>
              <Field label="主机" required hint="只填主机名或 IP">
                <Input
                  value={form.host}
                  placeholder="192.0.2.10"
                  onChange={(e) => setForm({ ...form, host: e.target.value })}
                />
              </Field>
              <Field label="端口" required>
                <Input
                  type="number"
                  min={1}
                  max={65535}
                  value={form.port}
                  onChange={(e) => setForm({ ...form, port: Number(e.target.value) })}
                />
              </Field>
              <Field label="认证用户名">
                <Input
                  value={form.username}
                  placeholder="无认证可留空"
                  onChange={(e) => setForm({ ...form, username: e.target.value })}
                />
              </Field>
              <Field
                label="密码"
                hint={editing?.has_password ? '已保存；留空表示不修改' : '无认证可留空'}
              >
                <Input
                  type="password"
                  autoComplete="new-password"
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                />
              </Field>
              <Field label="备注">
                <Input
                  value={form.note}
                  placeholder="用途或负责人"
                  onChange={(e) => setForm({ ...form, note: e.target.value })}
                />
              </Field>
              <Field label="状态">
                <label className="flex h-[30px] items-center gap-[8px] text-sm text-foreground">
                  <Switch checked={form.enabled} onChange={(next) => setForm({ ...form, enabled: next })} />
                  {form.enabled ? '启用' : '停用'}
                </label>
              </Field>
            </div>
            {formError && <div className="field-error mt-[6px]">{formError}</div>}
            <div className="mt-[10px] flex items-center gap-[8px]">
              <Button
                variant="primary"
                loading={createProxy.isPending || updateProxy.isPending}
                onClick={() => void submitForm()}
              >
                {editing ? '保存修改' : '保存并应用'}
              </Button>
              <Button
                variant="ghost"
                onClick={() => {
                  setFormOpen(false)
                  setEditing(null)
                }}
              >
                取消
              </Button>
              <span className="text-xs text-muted-foreground">
                保存后会重写 SearXNG 配置并重启，约 1–2 秒检索不可用
              </span>
            </div>
          </div>
        )}

        <DataTable
          columns={columns}
          rows={proxies}
          rowKey={(row) => row.id}
          loading={query.isLoading}
          empty={
            <EmptyState
              compact
              icon="Globe"
              title="还没有代理条目"
              description="新增代理后，可在下方把 SearXNG 引擎或天工抓取的域名绑定到它；未绑定的资源保持直连。"
              action={
                <Button variant="primary" quiet icon="Plus" onClick={openCreate}>
                  新增代理
                </Button>
              }
            />
          }
        />
      </Panel>

      <Panel
        title="按资源绑定"
        icon="Plug"
        padded
        meta={applies ? '一个资源只能绑一个代理' : '读取中'}
        actions={
          <Button
            variant="primary"
            quiet
            loading={saveBindings.isPending}
            disabled={!local}
            onClick={() => void submitBindings()}
          >
            保存绑定
          </Button>
        }
      >
        <div className="flex flex-col gap-[12px]">
          <div className="flex items-start gap-[6px] text-xs leading-[1.6] text-muted-foreground">
            <Icon name="Info" size={13} className="mt-[2px] flex-none" />
            <span>
              保存绑定会按仓库模板全量重写 SearXNG 的 settings.yml：未绑定的引擎走直连，
              手工改动会被覆盖。天工抓取按「精确域名 &gt; 最左通配 &gt; 来源站 &gt; 直连」匹配。
            </span>
          </div>

          <div>
            <SectionLabel>搜索引擎</SectionLabel>
            <div className="mt-[6px] flex flex-col divide-y divide-border rounded-md border border-border">
              {(state?.engines ?? []).map((row) => (
                <div key={row.name} className="flex flex-wrap items-center gap-[10px] px-[10px] py-[8px]">
                  <span className="mono min-w-[120px] text-sm text-foreground">{row.name}</span>
                  <Select
                    className="max-w-[260px]"
                    value={local?.engines[row.name] ?? ''}
                    onChange={(e) => {
                      if (!local) return
                      setLocal({
                        ...local,
                        engines: {
                          ...local.engines,
                          [row.name]: e.target.value ? Number(e.target.value) : null,
                        },
                      })
                    }}
                  >
                    <option value="">直连</option>
                    {enabledProxies.map((proxy) => (
                      <option key={proxy.id} value={proxy.id}>
                        {proxy.name}（{proxy.scheme}）
                      </option>
                    ))}
                  </Select>
                  <span className="toolbar-spacer" />
                  {(local?.engines[row.name] ?? null) !== row.proxy_id ? (
                    <Badge tone="neutral">待保存</Badge>
                  ) : row.applied ? (
                    <Badge tone="success" dot>已生效</Badge>
                  ) : (
                    <Badge tone="warning" dot>未生效</Badge>
                  )}
                </div>
              ))}
            </div>
          </div>

          <div>
            <SectionLabel>天工正文抓取</SectionLabel>
            <div className="mt-[6px] flex flex-col gap-[6px]">
              {local?.tiangong.length === 0 && (
                <div className="rounded-md border border-dashed border-border px-[10px] py-[8px] text-xs text-muted-foreground">
                  暂无绑定：所有域名直连。域名支持精确匹配与最左侧通配（*.example.com）。
                </div>
              )}
              {local?.tiangong.map((row, index) => {
                const proxy = proxies.find((p) => p.id === row.proxy_id)
                const socks5 = proxy?.scheme === 'socks5'
                return (
                  <div key={`${row.kind}:${row.key}`} className="flex flex-wrap items-center gap-[10px] rounded-md border border-border px-[10px] py-[8px]">
                    <span className="mono min-w-[180px] truncate text-sm">{row.key}</span>
                    <Badge tone={row.kind === 'domain' ? 'neutral' : 'primary'}>
                      {row.kind === 'domain' ? '域名' : '来源站'}
                    </Badge>
                    <Select
                      className="max-w-[260px]"
                      value={row.proxy_id}
                      disabled={socks5}
                      onChange={(e) => {
                        if (!local) return
                        const next = [...local.tiangong]
                        next[index] = { ...row, proxy_id: Number(e.target.value) }
                        setLocal({ ...local, tiangong: next })
                      }}
                    >
                      {httpProxies.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                        </option>
                      ))}
                      {socks5 && proxy && <option value={proxy.id}>{proxy.name}（SOCKS5 不支持）</option>}
                    </Select>
                    {socks5 && <span className="text-xs text-state-warning">天工只支持 http 代理</span>}
                    <span className="toolbar-spacer" />
                    {tiangongApplied.get(`${row.kind}:${row.key}`) ? (
                      <Badge tone="success" dot>已生效</Badge>
                    ) : (
                      <Badge tone="warning" dot>未生效</Badge>
                    )}
                    <IconButton icon="Trash2" label="删除绑定" onClick={() => removeTiangong(index)} />
                  </div>
                )
              })}
            </div>

            <div className="mt-[8px] flex flex-wrap items-end gap-[8px]">
              <Field label="新增域名" className="w-[220px]">
                <Input
                  value={newDomain}
                  placeholder="example.com 或 *.example.com"
                  onChange={(e) => setNewDomain(e.target.value)}
                />
              </Field>
              <Field label="代理" className="w-[220px]">
                <Select
                  value={newDomainProxy}
                  onChange={(e) => setNewDomainProxy(e.target.value ? Number(e.target.value) : '')}
                >
                  <option value="">选择代理</option>
                  {httpProxies.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Button quiet icon="Plus" onClick={addDomain}>
                添加
              </Button>
              {enabledProxies.length === 0 && (
                <span className="text-xs text-muted-foreground">先新增并启用一个 http 代理</span>
              )}
            </div>

            {state?.sources?.length ? (
              <div className="mt-[8px] text-xs text-muted-foreground">
                来源站绑定随来源库（L4）一同启用；当前已登记 {state.sources.length} 个来源站。
              </div>
            ) : (
              <div className="mt-[8px] text-xs text-muted-foreground">
                来源库（L4）尚未落地：暂不支持按来源站绑定，先按域名绑定。
              </div>
            )}
            {bindingError && <div className="field-error mt-[6px]">{bindingError}</div>}
          </div>
        </div>
      </Panel>

      <ConfirmDialog
        open={Boolean(deleting)}
        title="删除代理"
        confirmText="确认删除"
        tone="danger"
        loading={deleteProxy.isPending}
        message={
          <>
            删除「{deleting?.name}」会同时移除它的绑定。若仍有资源绑定，后端会拒绝删除，先解绑再操作。
          </>
        }
        onConfirm={() => void confirmDelete()}
        onCancel={() => setDeleting(null)}
      />
    </>
  )
}
