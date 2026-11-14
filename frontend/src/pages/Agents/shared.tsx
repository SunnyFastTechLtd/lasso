import { Badge } from '@/components/ui/Badge'
import type { AgentStatus, AgentVisibility } from '@/types/agent'

const STATUS_TONE: Record<AgentStatus, 'neutral' | 'success' | 'warning' | 'error'> = {
  draft: 'neutral',
  published: 'success',
  disabled: 'warning',
  archived: 'neutral',
}

export function AgentStatusBadge({ status, display }: { status: AgentStatus; display?: string }) {
  const labels: Record<AgentStatus, string> = {
    draft: '草稿',
    published: '已发布',
    disabled: '已停用',
    archived: '已归档',
  }
  return (
    <Badge tone={STATUS_TONE[status]} dot={status === 'published'}>
      {display || labels[status]}
    </Badge>
  )
}

/** 改了未重新发布标记（7.3：即时生效≠自动生效，须显式发布） */
export function PendingChangeBadge() {
  return (
    <Badge tone="warning" title="配置已修改但尚未重新发布，线上仍是旧版本">
      未发布改动
    </Badge>
  )
}

const VISIBILITY_LABEL: Record<AgentVisibility, string> = {
  private: '仅自己',
  department: '部门',
  public: '全公司',
  admin_only: '仅管理端',
}

/** A6 仅管理端标记：业务用户不可见，便于管理端列表区分 */
export function AdminOnlyBadge() {
  return (
    <Badge tone="neutral" title="仅管理端：业务用户不可见">
      仅管理端
    </Badge>
  )
}

export function VisibilityText({ visibility, display }: { visibility: AgentVisibility; display?: string }) {
  return (
    <span className="text-xs text-muted-foreground">
      {display || VISIBILITY_LABEL[visibility] || visibility}
    </span>
  )
}

/** 挂载摘要：工具/技能/知识库计数 */
export function MountSummary({ m }: { m: { mcp_servers: number; skills: number; knowledge_bases: number } }) {
  const total = m.mcp_servers + m.skills + m.knowledge_bases
  if (!total) return <span className="text-xs text-muted-foreground">未挂载</span>
  return (
    <span className="text-xs text-muted-foreground tnum">
      {m.mcp_servers} 工具源 · {m.skills} 技能 · {m.knowledge_bases} 知识库
    </span>
  )
}

export const JOB_TAG_PRESETS = ['行政', '人力', '财务', '知识管理']

export const ICON_PRESETS = [
  'Bot',
  'BookOpen',
  'Briefcase',
  'Calculator',
  'Scale',
  'HeartPulse',
  'Headset',
  'FileText',
  'GraduationCap',
  'PenTool',
]
