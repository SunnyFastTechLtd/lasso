import { useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { Field, Input, Select, Textarea } from '@/components/ui/Field'
import { Panel, SectionLabel } from '@/components/ui/Panel'
import { Seg } from '@/components/ui/Seg'
import { Switch } from '@/components/ui/Switch'
import { Drawer } from '@/components/ui/Overlay'
import { Icon } from '@/components/ui/Icon'
import { useCommandPrefixes, useSaveMcpServer } from '@/api/hooks/useMcp'
import { useDepartments } from '@/api/departments'
import { toast } from '@/store/toast'
import type { ConnectivityTest, McpLifecycle, McpServer, McpServerPayload, McpTransport, McpVisibility } from '@/types/mcp'
import { extractFieldErrors, KvEditor, rowsToKv, kvToRows, type KvRow } from './shared'

const VISIBILITIES: { value: McpVisibility; label: string }[] = [
  { value: 'private', label: '仅自己' },
  { value: 'department', label: '指定部门' },
  { value: 'public', label: '全企业' },
  { value: 'admin_only', label: '仅管理端' },
]

const LIFECYCLES: { value: McpLifecycle; label: string }[] = [
  { value: 'per_session', label: '每次独立会话' },
  { value: 'persistent', label: '保持会话/常驻' },
  { value: 'ephemeral', label: '会话结束即回收' },
]

interface Props {
  open: boolean
  /** null＝新增 */
  server: McpServer | null
  onClose: () => void
}

/** B1/B2/B5 新增与编辑抽屉：命令白名单只读、密钥掩码回填、超时留空走平台固定值 */
export function ServerForm({ open, server, onClose }: Props) {
  const isEdit = server !== null
  const builtinLocked = server?.is_builtin ?? false

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [transport, setTransport] = useState<McpTransport>('http_mcp')
  const [url, setUrl] = useState('')
  const [egressDomainsText, setEgressDomainsText] = useState('')
  const [command, setCommand] = useState('')
  const [argsText, setArgsText] = useState('')
  const [cwd, setCwd] = useState('')
  const [headers, setHeaders] = useState<KvRow[]>([])
  const [isStateful, setIsStateful] = useState(false)
  const [lifecycle, setLifecycle] = useState<McpLifecycle>('per_session')
  const [timeoutText, setTimeoutText] = useState('')
  const [visibility, setVisibility] = useState<McpVisibility>('private')
  const [deptIds, setDeptIds] = useState<number[]>([])
  const [secretHeaders, setSecretHeaders] = useState<KvRow[]>([])
  const [secretEnv, setSecretEnv] = useState<KvRow[]>([])
  const [formErrors, setFormErrors] = useState<string[]>([])

  // 掩码密钥的初始快照：未改动则不提交 secrets，避免误清空
  const secretsSnapshot = useRefJson(server)

  useEffect(() => {
    if (!open) return
    setFormErrors([])
    setName(server?.name ?? '')
    setDescription(server?.description ?? '')
    setTransport(server?.transport ?? 'http_mcp')
    setUrl(server?.url ?? '')
    setEgressDomainsText((server?.egress_domains ?? []).join('\n'))
    setCommand(server?.command ?? '')
    setArgsText((server?.args ?? []).join('\n'))
    setCwd(server?.cwd ?? '')
    setHeaders(kvToRows(server?.headers))
    setIsStateful(server?.is_stateful ?? false)
    setLifecycle(server?.lifecycle ?? 'per_session')
    setTimeoutText(server?.timeout_seconds == null ? '' : String(server.timeout_seconds))
    setVisibility(server?.visibility ?? 'private')
    setSecretHeaders(kvToRows(server?.secrets_masked?.headers))
    setSecretEnv(kvToRows(server?.secrets_masked?.env))
  }, [open, server])

  const { data: departments } = useDepartments(open && visibility === 'department')

  // 编辑态回填部门 ID（读取面只给部门名）
  useEffect(() => {
    if (!open || !server || !departments) return
    const ids = server.allowed_departments
      .map((n) => departments.find((d) => d.name === n)?.id)
      .filter((id): id is number => id !== undefined)
    setDeptIds(ids)
  }, [open, server, departments])

  const { data: prefixes } = useCommandPrefixes(open && transport === 'stdio')

  const save = useSaveMcpServer()

  const stdioPreview = useMemo(() => {
    if (transport !== 'stdio') return ''
    const argv = [command.trim(), ...argsText.split('\n').map((s) => s.trim()).filter(Boolean)].filter(Boolean)
    return argv.join(' ')
  }, [transport, command, argsText])

  async function submit() {
    if (!name.trim()) {
      setFormErrors(['请填写来源名称'])
      return
    }
    const args = argsText.split('\n').map((s) => s.trim()).filter(Boolean)
    const payload: McpServerPayload = {
      name: name.trim(),
      description: description.trim(),
      transport,
      url: transport === 'http_mcp' ? url.trim() : '',
      egress_domains: transport === 'http_mcp'
        ? egressDomainsText.split('\n').map((s) => s.trim()).filter(Boolean)
        : [],
      command: transport === 'stdio' ? command.trim() : '',
      args: transport === 'stdio' ? args : [],
      cwd: transport === 'stdio' ? cwd.trim() : '',
      headers: rowsToKv(headers),
      is_stateful: transport === 'stdio' ? true : isStateful,
      lifecycle: transport === 'stdio' ? 'persistent' : lifecycle,
      timeout_seconds: timeoutText.trim() === '' ? null : Number(timeoutText),
      visibility,
      allowed_department_ids: visibility === 'department' ? deptIds : [],
    }

    // B5：密钥仅在改动（或新增且有填写）时整组提交；掩码值由后端回填原值
    const current = JSON.stringify({ headers: rowsToKv(secretHeaders), env: rowsToKv(secretEnv) })
    const touched = !isEdit
      ? secretHeaders.some((r) => r.key.trim()) || secretEnv.some((r) => r.key.trim())
      : current !== secretsSnapshot.current
    if (touched) {
      payload.secrets = { headers: rowsToKv(secretHeaders), env: rowsToKv(secretEnv) }
    }

    setFormErrors([])
    try {
      const res = await save.mutateAsync({ id: server?.id ?? null, payload })
      const t = (res as McpServer & { connectivity_test?: ConnectivityTest }).connectivity_test
      if (!isEdit && t) {
        if (t.ok) toast.success(`已保存并通过连通测试：发现 ${t.tools.length} 个工具（新工具默认关闭）`)
        else toast.warning(`已保存，但连通测试失败：${t.failure_label || '未知错误'}——${t.failure_message}`)
      } else {
        toast.success('已更新；连接参数变更时需重新测试')
      }
      onClose()
    } catch (err) {
      const body = (err as { body?: Record<string, unknown> | null }).body ?? null
      const errs = extractFieldErrors(body)
      setFormErrors(errs.length ? errs : [(err as Error).message || '保存失败'])
    }
  }

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={server ? `编辑来源 · ${server.name}` : '新增工具来源'}
      meta={server ? `标识 ${server.slug} · 工具全称 mcp__${server.slug}__{tool}` : '保存后平台自动执行一次连通测试'}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={save.isPending}>
            取消
          </Button>
          <Button variant="primary" icon="Check" loading={save.isPending} onClick={submit}>
            {isEdit ? '保存修改' : '保存并测试'}
          </Button>
        </>
      }
    >
      {formErrors.length > 0 && (
        <div className="mb-[10px] rounded-md border border-state-error-border bg-state-error-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-error">
          {formErrors.map((e, i) => (
            <div key={i}>{e}</div>
          ))}
        </div>
      )}

      <div className="col-stack">
        <Panel title="基本信息" icon="Info" padded>
          <div className="form-grid">
            <Field label="名称" required hint="改名会破坏已有授权规则，重名将被拒绝">
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：企业知识检索" maxLength={128} />
            </Field>
            <Field label="可见范围" required hint="非管理员按 公开/自己创建/命中部门 过滤可见">
              <Select value={visibility} onChange={(e) => setVisibility(e.target.value as McpVisibility)}>
                {VISIBILITIES.map((v) => (
                  <option key={v.value} value={v.value}>
                    {v.label}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="描述" className="span-full">
              <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="一句话说明用途" maxLength={200} />
            </Field>
            {visibility === 'department' && (
              <Field label="可见部门" required className="span-full" hint="含子部门口径由装配页选择器实现">
                {departments ? (
                  <div className="flex flex-wrap gap-[6px]">
                    {departments.map((d) => (
                      <button
                        key={d.id}
                        type="button"
                        className="chip"
                        data-active={String(deptIds.includes(d.id))}
                        onClick={() =>
                          setDeptIds((prev) => (prev.includes(d.id) ? prev.filter((x) => x !== d.id) : [...prev, d.id]))
                        }
                      >
                        {d.name}
                      </button>
                    ))}
                    {departments.length === 0 && <span className="text-xs text-muted-foreground">暂无部门，请先在系统设置建档</span>}
                  </div>
                ) : (
                  <span className="text-xs text-muted-foreground">读取部门…</span>
                )}
              </Field>
            )}
          </div>
        </Panel>

        <Panel title="连接方式" icon="Plug" padded>
          <div className="flex flex-col gap-[10px]">
            <Seg
              items={[
                { value: 'http_mcp', label: '接口型（HTTP MCP）' },
                { value: 'stdio', label: '命令型（stdio）' },
              ]}
              value={transport}
              onChange={(v) => setTransport(v as McpTransport)}
              className="self-start"
            />
            {builtinLocked && (
              <div className="field-hint">
                <Icon name="Lock" size={12} className="mr-[4px] inline" />
                内置演示来源不可编辑地址与命令，只能改名称、描述与工具启停
              </div>
            )}

            {transport === 'http_mcp' ? (
              <>
                <Field label="服务地址" required hint="http/https；公网地址将被标记为出网类来源（B8），管理员不可自行取消">
                  <Input
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    placeholder="http://127.0.0.1:8001/mcp"
                    disabled={builtinLocked}
                  />
                </Field>
                <Field
                  label="出网允许域名（每行一个）"
                  hint="出网类来源必填，必须覆盖服务地址主机；支持精确域名与 *.example.com 通配。严格白名单开启后，未声明的域名与解析到私网段的地址都会被拦截"
                >
                  <Textarea
                    rows={3}
                    value={egressDomainsText}
                    onChange={(e) => setEgressDomainsText(e.target.value)}
                    placeholder={'api.example.com\n*.example.com'}
                    disabled={builtinLocked}
                  />
                </Field>
                <Field label="请求头（非敏感）" hint="敏感值请放下方密钥分组，含掩码形态的值会被拒绝">
                  <KvEditor rows={headers} onChange={setHeaders} keyPlaceholder="如 Authorization" valuePlaceholder="值" />
                </Field>
                <div className="form-grid">
                  <Field label="连接方式" hint="常驻复用＝跨会话保持连接">
                    <Select value={lifecycle} onChange={(e) => setLifecycle(e.target.value as McpLifecycle)} disabled={builtinLocked}>
                      {LIFECYCLES.map((l) => (
                        <option key={l.value} value={l.value}>
                          {l.label}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="有状态连接">
                    <div className="flex h-[30px] items-center">
                      <Switch checked={isStateful} onChange={setIsStateful} label={isStateful ? '有状态' : '无状态'} />
                    </div>
                  </Field>
                </div>
              </>
            ) : (
              <>
                <Field label="启动命令" required hint="与参数拼成命令行后须命中下方白名单前缀，否则保存被阻断（B2）">
                  <Input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="/path/to/.venv/bin/python" disabled={builtinLocked} />
                </Field>
                <Field label="启动参数（每行一个）">
                  <Textarea rows={3} value={argsText} onChange={(e) => setArgsText(e.target.value)} placeholder={'/srv/mcp/demo_server.py\n--port 9001'} disabled={builtinLocked} />
                </Field>
                <Field label="工作目录" hint="留空使用平台默认目录">
                  <Input value={cwd} onChange={(e) => setCwd(e.target.value)} placeholder="/srv/mcp" disabled={builtinLocked} />
                </Field>
                {stdioPreview && (
                  <div className="field-hint">
                    命令行预览：<span className="mono">{stdioPreview}</span>
                  </div>
                )}
                <div className="field-hint">
                  命令型的连接方式已锁定为「保持会话/常驻」（B2 例外口径），界面不提供选择
                </div>

                <div>
                  <SectionLabel>允许的命令前缀（平台内置只读）</SectionLabel>
                  {prefixes ? (
                    <div className="col-stack mt-[6px] gap-[4px]">
                      <div className="field-hint">{prefixes.policy}</div>
                      {prefixes.prefixes.map((p) => (
                        <div key={p.prefix} className="list-row">
                          <span className="row-main min-w-0">
                            <span className="row-title mono truncate">{p.prefix}</span>
                            <span className="row-sub">{p.usage}</span>
                          </span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="text-xs text-muted-foreground">读取白名单…</div>
                  )}
                </div>
              </>
            )}

            <Field
              label="超时秒数"
              hint={`留空按平台固定值：接口型 30 秒 / 命令型 90 秒；填写取 5–600 秒（当前生效 ${server?.effective_timeout ?? (transport === 'stdio' ? 90 : 30)} 秒）`}
            >
              <Input
                type="number"
                min={5}
                max={600}
                value={timeoutText}
                onChange={(e) => setTimeoutText(e.target.value)}
                placeholder={transport === 'stdio' ? '留空＝90' : '留空＝30'}
              />
            </Field>
          </div>
        </Panel>

        <Panel title="密钥（B5 加密存储，只回显掩码）" icon="KeyRound" padded>
          <div className="flex flex-col gap-[10px]">
            <div className="field-hint">
              值形如 ******abcd 为掩码占位：不改动则保留原值；删除整行即删除该键；改动值＝整值替换。密钥不进模型上下文、不落审计。
            </div>
            <div>
              <SectionLabel>认证请求头</SectionLabel>
              <div className="mt-[6px]">
                <KvEditor rows={secretHeaders} onChange={setSecretHeaders} keyPlaceholder="如 X-API-Key" valuePlaceholder="值或保持掩码" />
              </div>
            </div>
            <div>
              <SectionLabel>进程环境变量（命令型）</SectionLabel>
              <div className="mt-[6px]">
                <KvEditor rows={secretEnv} onChange={setSecretEnv} keyPlaceholder="如 DEMO_TOKEN" valuePlaceholder="值或保持掩码" />
              </div>
            </div>
          </div>
        </Panel>
      </div>
    </Drawer>
  )
}

/** 用 ref 记录密钥初始快照（避免额外 state） */
function useRefJson(server: McpServer | null) {
  const ref = useMemo(
    () => ({
      current: JSON.stringify({
        headers: rowsToKv(kvToRows(server?.secrets_masked?.headers)),
        env: rowsToKv(kvToRows(server?.secrets_masked?.env)),
      }),
    }),
    [server],
  )
  return ref
}
