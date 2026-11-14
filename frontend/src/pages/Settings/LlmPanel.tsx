import { useEffect, useRef, useState } from 'react'
import { ApiError } from '@/api/request'
import {
  useResetLlmConfig,
  useTestLlmConfig,
  useUpdateLlmConfig,
  type LlmConfig,
  type LlmHealth,
} from '@/api/settings'
import { Badge } from '@/components/ui/Badge'
import { Button, IconButton } from '@/components/ui/Button'
import { CodeBlock } from '@/components/ui/CodeBlock'
import { Field, Input } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { ConfirmDialog } from '@/components/ui/Overlay'
import { Panel } from '@/components/ui/Panel'
import { toast } from '@/store/toast'
import { HEALTH_LABEL } from './shared'

interface LlmForm {
  base_url: string
  api_key: string
  model: string
  temperature: string
  max_tokens: string
  timeout: string
}

const EMPTY_FORM: LlmForm = { base_url: '', api_key: '', model: '', temperature: '', max_tokens: '', timeout: '' }

const toForm = (c: LlmConfig): LlmForm => ({
  base_url: c.base_url,
  api_key: '',
  model: c.model,
  temperature: String(c.temperature),
  max_tokens: String(c.max_tokens),
  timeout: String(c.timeout),
})

/** 脏检查只看可回显字段：密钥是只写字段，不参与与服务端值的比对 */
const signature = (f: LlmForm) => [f.base_url, f.model, f.temperature, f.max_tokens, f.timeout].join('|')

type FormErrors = Partial<Record<keyof LlmForm, string>>

function validate(f: LlmForm): FormErrors {
  const errors: FormErrors = {}
  if (!/^https?:\/\/\S+$/i.test(f.base_url.trim())) errors.base_url = '请填写 http(s):// 开头的完整接口地址'
  if (!f.model.trim()) errors.model = '模型名不能为空'
  // 后端 serializer 的 api_key 必填且整值覆盖落库，留空等于清空现网密钥
  if (!f.api_key.trim()) errors.api_key = '保存会整体覆盖现有密钥，请填写完整 API Key'
  const temp = Number(f.temperature)
  if (!f.temperature.trim() || Number.isNaN(temp) || temp < 0 || temp > 2) errors.temperature = '取值范围 0 ~ 2'
  const maxTokens = Number(f.max_tokens)
  if (!f.max_tokens.trim() || !Number.isInteger(maxTokens) || maxTokens < 256) errors.max_tokens = '不小于 256 的整数'
  const timeout = Number(f.timeout)
  if (!f.timeout.trim() || !Number.isInteger(timeout) || timeout < 10) errors.timeout = '不小于 10 的整数（秒）'
  return errors
}

export function LlmPanel({ config, loading }: { config?: LlmConfig; loading: boolean }) {
  const [form, setForm] = useState<LlmForm>(EMPTY_FORM)
  const [errors, setErrors] = useState<FormErrors>({})
  const [health, setHealth] = useState<LlmHealth | null>(null)
  const [resetOpen, setResetOpen] = useState(false)
  const syncedRef = useRef('')

  const update = useUpdateLlmConfig()
  const test = useTestLlmConfig()
  const reset = useResetLlmConfig()

  // 服务端配置变化（首帧、保存、重置）后回到干净态
  useEffect(() => {
    if (!config) return
    const next = toForm(config)
    const sig = signature(next)
    if (sig !== syncedRef.current) {
      syncedRef.current = sig
      setForm(next)
      setErrors({})
    }
  }, [config])

  const dirty = signature(form) !== syncedRef.current || form.api_key.trim() !== ''
  const busy = update.isPending || test.isPending || reset.isPending
  const healthOk = health?.status === 'ok'
  const verified = config?.verified_models ?? []

  function patch(key: keyof LlmForm, value: string) {
    setForm((prev) => ({ ...prev, [key]: value }))
    setErrors((prev) => (prev[key] ? { ...prev, [key]: undefined } : prev))
  }

  async function save() {
    const found = validate(form)
    setErrors(found)
    if (Object.values(found).some(Boolean)) {
      toast.warning('请先补全被标记的必填项')
      return
    }
    try {
      const res = await update.mutateAsync({
        base_url: form.base_url.trim(),
        api_key: form.api_key.trim(),
        model: form.model.trim(),
        temperature: Number(form.temperature),
        max_tokens: Number(form.max_tokens),
        timeout: Number(form.timeout),
      })
      setHealth(res.health)
      if (res.saved && res.health.status === 'ok') {
        toast.success('健康检查通过，配置已保存并生效')
      } else {
        // 后端检查未通过时不落库：现网仍是原配置
        toast.error(`${HEALTH_LABEL[res.health.status] ?? '健康检查失败'}，本次修改未保存`)
      }
    } catch (err) {
      toast.error((err as ApiError).message)
    }
  }

  async function runTest() {
    try {
      const res = await test.mutateAsync()
      setHealth(res)
      if (res.status === 'ok') {
        toast.success(`${res.verified_model || res.current_model || '当前模型'} 连通正常，已进入可用模型列表`)
      } else {
        toast.warning(`健康检查未通过：${HEALTH_LABEL[res.status] ?? res.status}`)
      }
    } catch (err) {
      toast.error((err as ApiError).message)
    }
  }

  async function confirmReset() {
    try {
      const res = await reset.mutateAsync()
      toast.success(res.detail || '已清空数据库内的模型配置')
      setHealth(null)
      setResetOpen(false)
    } catch (err) {
      toast.error((err as ApiError).message)
    }
  }

  return (
    <Panel
      title="模型接入与凭据"
      icon="Cpu"
      padded
      meta={
        health
          ? healthOk
            ? '本次检查通过'
            : '本次检查未通过'
          : verified.length
            ? `已验证 ${verified.length} 个模型`
            : '尚无已验证模型'
      }
      actions={
        <Button quiet variant="ghost" icon="PlugZap" onClick={runTest} loading={test.isPending} disabled={busy && !test.isPending}>
          测试连接
        </Button>
      }
      footer={
        <>
          <span className="flex flex-wrap items-center gap-[8px] py-[3px]">
            <Button variant="primary" icon="Check" onClick={save} loading={update.isPending} disabled={busy && !update.isPending}>
              保存并健康检查
            </Button>
            <Button variant="ghost" icon="RotateCcw" disabled={!dirty || busy} onClick={() => setResetOpen(true)}>
              清空模型配置
            </Button>
            {dirty && (
              <Button
                variant="ghost"
                disabled={busy}
                onClick={() => {
                  if (config) {
                    setForm(toForm(config))
                    setErrors({})
                  }
                }}
              >
                放弃修改
              </Button>
            )}
          </span>
          <span className="toolbar-note">
            {dirty ? <span className="text-state-warning">有未保存的修改</span> : '与已保存配置一致'}
          </span>
        </>
      }
    >
      <div className="flex flex-col gap-[12px]">
        <p className="text-xs leading-[1.6] text-muted-foreground">
          F1 · 模型配置只来自这里的数据库记录，环境变量不参与模型选择或兜底。
          点保存时平台先用你填写的配置做一次健康检查（真实发起一次补全调用），通过才落库生效；
          未通过则本次修改不保存，现网仍按原配置服务。只有检查通过的模型会进入智能体的模型下拉。
          密钥仅以掩码回显，明文不回流前端。
        </p>

        {config && config.configured === false && (
          <div className="flex items-start gap-[6px] text-xs leading-[1.6] text-state-warning">
            <Icon name="TriangleAlert" size={13} className="mt-[2px] flex-none text-state-warning" />
            <span>
              {config.hint || '模型接入尚未配置完整，补齐后平台才能调用模型。'}
              {config.missing_fields?.length
                ? ` 缺少：${config.missing_fields.map((k) => ({ llm_base_url: 'API 地址', llm_api_key: 'API Key', llm_model: '模型名' }[k] ?? k)).join('、')}。`
                : ''}
            </span>
          </div>
        )}

        {loading && !config ? (
          <div className="flex items-center gap-[8px] py-[18px] text-muted-foreground">
            <Icon name="LoaderCircle" size={14} className="animate-spin" />
            <span className="text-xs">读取当前接入…</span>
          </div>
        ) : (
          <div className="form-grid">
            <Field className="span-full" label="Base URL" required error={errors.base_url}>
              <Input
                className="mono"
                value={form.base_url}
                invalid={!!errors.base_url}
                placeholder="https://内网网关/compatible-mode/v1"
                onChange={(e) => patch('base_url', e.target.value)}
              />
            </Field>

            <Field label="模型名" required error={errors.model}>
              <Input
                className="mono"
                value={form.model}
                invalid={!!errors.model}
                placeholder="qwen3-32b"
                onChange={(e) => patch('model', e.target.value)}
              />
            </Field>

            <Field label="采样温度" required error={errors.temperature} hint="0 ~ 2">
              <Input
                className="tnum"
                type="number"
                step="0.1"
                min="0"
                max="2"
                value={form.temperature}
                invalid={!!errors.temperature}
                onChange={(e) => patch('temperature', e.target.value)}
              />
            </Field>

            <Field label="最大生成长度" required error={errors.max_tokens} hint="不小于 256">
              <Input
                className="tnum"
                type="number"
                min="256"
                value={form.max_tokens}
                invalid={!!errors.max_tokens}
                onChange={(e) => patch('max_tokens', e.target.value)}
              />
            </Field>

            <Field label="请求超时（秒）" required error={errors.timeout} hint="不小于 10，与后端 llm_timeout 同单位">
              <Input
                className="tnum"
                type="number"
                min="10"
                value={form.timeout}
                invalid={!!errors.timeout}
                onChange={(e) => patch('timeout', e.target.value)}
              />
            </Field>

            <Field
              className="span-full"
              label="API Key"
              required
              error={errors.api_key}
              hint={`当前掩码 ${config?.api_key_masked || '（未设置）'}：后端按整值覆盖保存，每次保存都需重新输入完整密钥`}
            >
              <Input
                className="mono"
                type="password"
                autoComplete="new-password"
                value={form.api_key}
                invalid={!!errors.api_key}
                placeholder="粘贴完整 API Key"
                onChange={(e) => patch('api_key', e.target.value)}
              />
            </Field>
          </div>
        )}

        {health && (
          <div
            className={`rounded-md border px-[10px] py-[9px] ${
              healthOk
                ? 'border-state-success/40 bg-state-success/10'
                : 'border-state-error/40 bg-state-error/10'
            }`}
          >
            <div className="flex items-center gap-[8px]">
              <Badge tone={healthOk ? 'success' : 'error'} dot>
                {healthOk
                  ? '连通正常 · 配置已生效'
                  : `${HEALTH_LABEL[health.status] ?? '健康检查失败'} · 本次未保存`}
              </Badge>
              <span className="mono truncate text-xs text-muted-foreground">{health.base_url || config?.base_url || '—'}</span>
              <IconButton icon="X" label="收起检查结果" className="ml-auto" onClick={() => setHealth(null)} />
            </div>

            {healthOk ? (
              <div className="mt-[8px] flex flex-col gap-[8px]">
                <div className="flex flex-wrap items-center gap-[16px] text-xs text-muted-foreground">
                  <span>
                    当前模型 <span className="mono text-sidebar-foreground">{health.current_model || '—'}</span>
                  </span>
                  <span>
                    可见模型 <span className="tnum text-sidebar-foreground">{health.model_count ?? 0}</span>
                  </span>
                </div>
                {health.available_models && health.available_models.length > 0 && (
                  <div className="flex flex-wrap gap-[4px]">
                    {health.available_models.slice(0, 12).map((m) => (
                      <span key={m} className="chip mono">
                        {m}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <div className="mt-[8px] flex flex-col gap-[8px]">
                <CodeBlock code={health.error || '后端未返回失败原因'} maxHeight={140} />
                <p className="text-xs leading-[1.6] text-muted-foreground">
                  本次检查未通过，配置没有落库，现网仍按原地址与原模型服务。修正 Base URL 或密钥后重新保存即可；
                  如需撤销整套接入凭据，用「清空模型配置」；清空后平台会明确拒绝调用模型，直到重新配置。
                </p>
              </div>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-[8px]">
          <span className="text-xs text-muted-foreground">智能体可用模型</span>
          {verified.length ? (
            verified.map((m) => (
              <span key={m} className="chip mono">
                {m}
              </span>
            ))
          ) : (
            <span className="text-xs text-muted-foreground">
              暂无，健康检查通过后自动列入；装配页此时只能选当前主模型
            </span>
          )}
        </div>

        <div className="flex items-start gap-[6px] text-xs leading-[1.6] text-muted-foreground">
          <Icon name="Info" size={13} className="mt-[2px] flex-none" />
          <span>
            「测试连接」探测已保存的配置并把该模型列入可用列表，表单中未保存的改动不参与测试；
            保存动作会以「更新 LLM 配置」写入审计（含未通过的原因），重置动作写入「重置 LLM 配置」。
          </span>
        </div>
      </div>

      <ConfirmDialog
        open={resetOpen}
        tone="danger"
        title="清空模型配置"
        message="删除数据库内的模型接入配置（含密钥）。环境变量不再作为兜底，清空后平台会明确拒绝调用模型，直到重新填写并保存一套完整配置。本次动作写入审计。"
        confirmText="确认清空"
        loading={reset.isPending}
        onConfirm={confirmReset}
        onCancel={() => setResetOpen(false)}
      />
    </Panel>
  )
}
