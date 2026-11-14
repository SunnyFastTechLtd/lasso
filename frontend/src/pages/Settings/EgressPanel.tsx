import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '@/api/request'
import { useSetEgress } from '@/api/hooks/useMcp'
import type { EgressState } from '@/types/mcp'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Icon } from '@/components/ui/Icon'
import { ConfirmDialog } from '@/components/ui/Overlay'
import { Panel, SectionLabel } from '@/components/ui/Panel'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/store/toast'

const CONSEQUENCES: { icon: string; title: string; desc: string }[] = [
  {
    icon: 'PlugZap',
    title: '所有云端来源不可调用',
    desc: '出网类（公网地址）工具来源在会话内一律拒绝执行，拒绝动作以「出网被出口清单拒绝」写入审计。',
  },
  {
    icon: 'Layers',
    title: '装配页对应来源置灰并带原因',
    desc: '智能体装配与技能挂载的来源选择器中，出网类来源不可选，状态显示「出网未开放」。',
  },
  {
    icon: 'Search',
    title: '出网类来源不可新增、不可发现工具',
    desc: '新增来源、连通性测试与刷新工具列表都会被闸门拦住，避免关闭期间混入新的云端能力。',
  },
  {
    icon: 'History',
    title: '历史会话仍可读',
    desc: '已产生的会话记录、审计留痕与用量数据不受影响，仅不再发起新的出网调用。',
  },
]

export function EgressPanel({ egress, loading }: { egress?: EgressState; loading: boolean }) {
  const [confirmOpen, setConfirmOpen] = useState(false)
  const setEgress = useSetEgress()
  const navigate = useNavigate()

  const enabled = egress?.enabled ?? false
  const strict = Boolean(egress?.strict_domains)

  async function apply(next: boolean) {
    try {
      const res = await setEgress.mutateAsync({ enabled: next })
      toast.success(res.enabled ? '出网已放开：受限来源恢复可达，请逐一重新测试' : '出网已关闭：出网类来源转入「出网未开放」')
    } catch (err) {
      toast.error((err as ApiError).message)
    } finally {
      setConfirmOpen(false)
    }
  }

  async function applyStrict(next: boolean) {
    try {
      await setEgress.mutateAsync({ strict_domains: next })
      toast.success(next ? '已开启严格域名白名单：出网来源只允许访问其声明域名' : '已关闭严格白名单：恢复原有行为（只受总开关约束）')
    } catch (err) {
      toast.error((err as ApiError).message)
    }
  }

  function requestChange(next: boolean) {
    if (loading || setEgress.isPending) return
    // 开启是放开红线的方向，需二次确认；关闭属止损动作，立即执行
    if (next) setConfirmOpen(true)
    else void apply(false)
  }

  return (
    <Panel
      title="出网总开关（B8）"
      icon={enabled ? 'ShieldCheck' : 'ShieldOff'}
      padded
      meta={loading && !egress ? '读取中' : enabled ? '已放开' : '已关闭'}
      actions={
        <Button quiet variant="ghost" icon="ExternalLink" onClick={() => navigate('/mcp')}>
          查看工具来源
        </Button>
      }
    >
      <div className="flex flex-col gap-[12px]">
        <div className="flex flex-wrap items-center gap-[10px] rounded-md border border-border bg-surface-sunken px-[10px] py-[9px]">
          <Switch checked={enabled} disabled={loading || setEgress.isPending} onChange={requestChange} />
          <span className="flex min-w-0 flex-col leading-tight">
            <span className="row-title">允许平台出网</span>
            <span className="row-sub">
              {enabled
                ? '出网类工具来源可被发现与调用；关闭后立即转入受限状态。'
                : '当前所有云端来源不可调用，装配页对应来源置灰并标注原因。'}
            </span>
          </span>
          <span className="toolbar-spacer" />
          {egress &&
            (enabled ? (
              <Badge tone="success" dot>
                出网开放
              </Badge>
            ) : (
              <Badge tone="warning" dot>
                受限来源 {egress.blocked_count} 个
              </Badge>
            ))}
        </div>

        <div className="flex flex-wrap items-center gap-[10px] rounded-md border border-border bg-surface-sunken px-[10px] py-[9px]">
          <Switch checked={strict} disabled={loading || setEgress.isPending} onChange={(next) => void applyStrict(next)} />
          <span className="flex min-w-0 flex-col leading-tight">
            <span className="row-title">严格域名白名单</span>
            <span className="row-sub">
              {strict
                ? '出网来源只允许访问其声明的域名，且解析到私网/保留地址会被拦截（防 DNS rebinding）。'
                : '关闭时沿用原有行为：只受全平台出网总开关约束，不校验声明域名。'}
            </span>
          </span>
          <span className="toolbar-spacer" />
          <Badge tone={strict ? 'success' : 'neutral'} dot>
            {strict ? '严格校验' : '沿用原行为'}
          </Badge>
        </div>

        <div className="flex items-start gap-[6px] text-xs leading-[1.6] text-muted-foreground">
          <Icon name="Info" size={13} className="mt-[2px] flex-none" />
          <span>出厂默认关闭：新环境需管理员在此显式放开。开关状态由后端返回，前端不做本地推断；变更即时生效于新会话，并以「切换出网总开关」写入审计。</span>
        </div>

        <div>
          <SectionLabel>关闭后的业务后果</SectionLabel>
          <div className="mt-[6px] flex flex-col gap-[6px]">
            {CONSEQUENCES.map((c) => (
              <div key={c.title} className="flex items-start gap-[8px] rounded-md border border-border px-[10px] py-[8px]">
                <Icon name={c.icon} size={14} className={enabled ? 'mt-[2px] flex-none text-muted-foreground' : 'mt-[2px] flex-none text-state-warning'} />
                <span className="flex min-w-0 flex-col gap-[1px]">
                  <span className="row-title">{c.title}</span>
                  <span className="row-sub">{c.desc}</span>
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={confirmOpen}
        title="放开全平台出网"
        confirmText="确认放开出网"
        loading={setEgress.isPending}
        message={
          <>
            放开后，所有出网类（公网地址）工具来源将恢复可发现、可调用，云端返回内容会进入模型上下文与生成链路。
            请确认已按 B8 口径复核这些来源。
          </>
        }
        onConfirm={() => void apply(true)}
        onCancel={() => setConfirmOpen(false)}
      />
    </Panel>
  )
}
