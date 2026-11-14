import { PageContainer, PageHeader } from '@/components/AppShell'
import { Panel } from '@/components/ui/Panel'
import { EmptyState } from '@/components/ui/EmptyState'

interface PlaceholderProps {
  title: string
  note: string
}

/** 本期只保留路由占位的功能，不伪造界面 */
export function PlaceholderPage({ title, note }: PlaceholderProps) {
  return (
    <PageContainer>
      <PageHeader title={title} subtitle={`${title}模块的既有路由占位。`} badge={<span className="badge badge-neutral">未开工</span>} />
      <Panel title="模块状态" icon="Hammer">
        <EmptyState icon="Hammer" title={note} description="该模块的路由与导航已就位，功能将在后续阶段实现。" />
      </Panel>
    </PageContainer>
  )
}
