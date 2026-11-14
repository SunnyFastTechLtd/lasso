import { useState } from 'react'
import { useEgress } from '@/api/hooks/useMcp'
import { useLlmConfig } from '@/api/settings'
import { PageContainer, PageHeader } from '@/components/AppShell'
import { Badge } from '@/components/ui/Badge'
import { EmptyState } from '@/components/ui/EmptyState'
import { Panel } from '@/components/ui/Panel'
import { Seg } from '@/components/ui/Seg'
import { useUserStore } from '@/store/user'
import { AppearancePanel } from './AppearancePanel'
import { AuditPanel } from './AuditPanel'
import { EgressPanel } from './EgressPanel'
import { LlmPanel } from './LlmPanel'
import { NetworkProxyPanel } from './NetworkProxyPanel'
import { SkillPackagePanel } from './SkillPackagePanel'

type SectionKey = 'llm' | 'egress' | 'network' | 'skill' | 'audit' | 'appearance'

const SECTIONS: { value: SectionKey; label: string }[] = [
  { value: 'llm', label: '模型接入与凭据' },
  { value: 'egress', label: '出网总开关' },
  { value: 'network', label: '网络代理' },
  { value: 'skill', label: '技能包附件' },
  { value: 'audit', label: '审计日志' },
  { value: 'appearance', label: '外观' },
]

export default function SettingsPage() {
  // 后端 User 序列化只回 is_staff，管理员口径与其余页面一致
  const isAdmin = useUserStore((s) => s.user?.is_staff ?? false)
  const [section, setSection] = useState<SectionKey>('llm')

  const llm = useLlmConfig(isAdmin)
  const egressQuery = useEgress(isAdmin)
  const egress = egressQuery.data
  const egressEnabled = egress?.enabled ?? false

  if (!isAdmin) {
    return (
      <PageContainer>
        <PageHeader
          title="系统设置"
          subtitle="模型接入与健康检查、全平台出网闸门、审计留痕；外观偏好对本机生效"
          actions={<Badge tone="neutral">使用端视图</Badge>}
        />
        <AppearancePanel />
        <Panel title="访问范围" icon="Lock" padded>
          <EmptyState
            icon="Lock"
            title="仅平台管理员可访问系统设置"
            description={
              '这里改动的是全平台共用的一套配置：模型接入凭据、出网总开关、以及所有人的操作留痕。' +
              '业务用户请在对话工作台使用已发布的智能体；需要调整配置请联系平台管理员。'
            }
          />
        </Panel>
      </PageContainer>
    )
  }

  return (
    <PageContainer>
      <PageHeader
        title="系统设置"
        subtitle="模型接入与健康检查、全平台出网闸门、审计留痕；外观偏好对本机生效"
        actions={
          <>
            <Badge tone="primary" dot>
              平台级配置
            </Badge>
            <Badge tone="neutral">仅管理员</Badge>
          </>
        }
      />

      <div className="grid grid-cols-2 gap-[10px] md:grid-cols-4">
        <div className="rounded-lg border border-border bg-card px-[12px] py-[10px]">
          <div className="text-xs text-muted-foreground">出网总开关</div>
          <div className={`metric-value ${egressEnabled ? 'text-state-success' : 'text-state-warning'}`}>
            {egressQuery.isLoading && !egress ? '…' : egressEnabled ? '开放' : '受限'}
          </div>
          <div className="mt-[2px] truncate text-xs text-muted-foreground">
            {egress ? `受限来源 ${egress.blocked_count} 个` : '读取中…'}
          </div>
        </div>
        <div className="rounded-lg border border-border bg-card px-[12px] py-[10px]">
          <div className="text-xs text-muted-foreground">当前模型</div>
          <div className="truncate text-sm font-semibold text-sidebar-foreground">
            {llm.data?.model || '—'}
          </div>
          <div className="mt-[2px] truncate mono text-xs text-muted-foreground" title={llm.data?.base_url}>
            {llm.data?.base_url || '读取中…'}
          </div>
        </div>
        <div className="rounded-lg border border-border bg-card px-[12px] py-[10px]">
          <div className="text-xs text-muted-foreground">采样参数</div>
          <div className="tnum text-sm font-semibold text-sidebar-foreground">
            {llm.data ? `t=${llm.data.temperature} · ${llm.data.max_tokens}` : '—'}
          </div>
          <div className="mt-[2px] text-xs text-muted-foreground">超时 {llm.data?.timeout ?? '—'} 秒</div>
        </div>
        <div className="rounded-lg border border-border bg-card px-[12px] py-[10px]">
          <div className="text-xs text-muted-foreground">API Key</div>
          <div className="mono truncate text-sm font-semibold text-sidebar-foreground">
            {llm.data?.api_key_masked || '—'}
          </div>
          <div className="mt-[2px] text-xs text-muted-foreground">仅掩码回显</div>
        </div>
      </div>

      <div className="page-toolbar">
        <Seg items={SECTIONS} value={section} onChange={setSection} />
        <span className="toolbar-spacer" />
        <span className="toolbar-note">
          {section === 'llm' && '保存先做健康检查：通过才落库并进入模型下拉，未通过则本次修改不保存'}
          {section === 'egress' && '一个全局闸门影响全部出网类（云端）来源'}
          {section === 'network' && '保存后自动重写 SearXNG 配置并重启（约 1–2 秒）；未绑定的资源走直连'}
          {section === 'skill' && '附件类型可配置；高危内容仍需上传者确认放行，可执行与二进制类型始终拒绝'}
          {section === 'audit' && '配置与装配动作倒序留痕，可按操作人、动作、对象、时间区间检索'}
          {section === 'appearance' && '深色为默认；皮肤与明暗独立组合，选择均保存在本机浏览器'}
        </span>
      </div>

      <div className="col-stack">
        {section === 'llm' && <LlmPanel config={llm.data} loading={llm.isLoading} />}
        {section === 'egress' && <EgressPanel egress={egress} loading={egressQuery.isLoading} />}
        {section === 'network' && <NetworkProxyPanel />}
        {section === 'skill' && <SkillPackagePanel />}
        {section === 'audit' && <AuditPanel />}
        {section === 'appearance' && <AppearancePanel />}
      </div>
    </PageContainer>
  )
}
