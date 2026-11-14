import { useEffect, useMemo, useState } from 'react'
import { revealSensitive } from '@/api/audit'
import { useAuditLogs } from '@/api/settings'
import { Button } from '@/components/ui/Button'
import { DataTable } from '@/components/ui/DataTable'
import { EmptyState } from '@/components/ui/EmptyState'
import { Input, Select } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { Pagination } from '@/components/ui/Pagination'
import { Panel } from '@/components/ui/Panel'
import { useSensitiveReveal } from '@/components/ui/SensitiveText'
import { useUserStore } from '@/store/user'
import type { AuditListParams, AuditLogItem } from '@/types/audit'
import { fmtDateTime } from '@/utils/format'
import { AUDIT_ACTION_GROUPS, AUDIT_ACTION_LABELS, AuditActionCell } from './shared'
import { canViewBody, ToolBodyDrawer } from './ToolBodyDrawer'

/** 详情列：默认掩码，展开原文前先写审计（沿用审计页的管理员判定） */
function AuditDetailCell({ row }: { row: AuditLogItem }) {
  const { shown, hits, revealed, busy, toggle } = useSensitiveReveal(row.detail || '', () =>
    revealSensitive({ scope: 'audit', audit_id: row.id, field: 'detail' }),
  )
  return (
    <div className="flex items-start gap-[8px]">
      <span className="block min-w-0 max-w-[520px] flex-1 truncate text-xs text-muted-foreground" title={shown}>
        {shown || '—'}
      </span>
      {hits > 0 && (
        <button
          type="button"
          className="flex flex-none items-center gap-[2px] whitespace-nowrap text-xs text-primary hover:underline disabled:opacity-60"
          disabled={busy}
          title={
            revealed
              ? '重新隐藏并保持脱敏展示'
              : `展开原文会记录查看人、时间与对象；本条命中 ${hits} 处敏感字段`
          }
          onClick={() => void toggle()}
        >
          <Icon name="Lock" size={12} />
          {busy ? '记录中…' : revealed ? '隐藏原文' : '显示原文'}
        </button>
      )}
    </div>
  )
}

/** 后端过滤参数：actor / action / target / date_start / date_end（未识别参数会被忽略） */
export function AuditPanel() {
  const [actorInput, setActorInput] = useState('')
  const [targetInput, setTargetInput] = useState('')
  const [actor, setActor] = useState('')
  const [target, setTarget] = useState('')
  const [action, setAction] = useState('')
  const [dateStart, setDateStart] = useState('')
  const [dateEnd, setDateEnd] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  // B4：工具调用全文抽屉；入口仅对管理员且有全文的行显示
  const [bodyRow, setBodyRow] = useState<AuditLogItem | null>(null)
  const isAdmin = useUserStore((s) => s.user?.is_staff ?? false)

  // 文本类条件走防抖，与其余管理页一致
  useEffect(() => {
    const t = setTimeout(() => {
      setActor(actorInput.trim())
      setTarget(targetInput.trim())
      setPage(1)
    }, 350)
    return () => clearTimeout(t)
  }, [actorInput, targetInput])

  const params: AuditListParams = useMemo(
    () => ({
      page,
      page_size: pageSize,
      ...(actor ? { actor } : {}),
      ...(action ? { action } : {}),
      ...(target ? { target } : {}),
      ...(dateStart ? { date_start: dateStart } : {}),
      ...(dateEnd ? { date_end: dateEnd } : {}),
    }),
    [page, pageSize, actor, action, target, dateStart, dateEnd],
  )

  const { data, isFetching, error } = useAuditLogs(params)
  const hasFilter = !!(actor || action || target || dateStart || dateEnd)

  function resetFilters() {
    setActorInput('')
    setTargetInput('')
    setAction('')
    setDateStart('')
    setDateEnd('')
    setPage(1)
  }

  return (
    <>
      <div className="page-toolbar">
        <span className="icon-btn">
          <Icon name="ScrollText" size={14} />
        </span>
        <Input
          className="w-[190px]"
          value={actorInput}
          onChange={(e) => setActorInput(e.target.value)}
          placeholder="按操作人检索"
        />
        <Select className="w-[190px]" value={action} onChange={(e) => {
          setAction(e.target.value)
          setPage(1)
        }}>
          <option value="">全部动作</option>
          {AUDIT_ACTION_GROUPS.map((g) => (
            <optgroup key={g.label} label={g.label}>
              {g.options.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </optgroup>
          ))}
        </Select>
        <Input
          className="w-[190px]"
          value={targetInput}
          onChange={(e) => setTargetInput(e.target.value)}
          placeholder="按对象检索"
        />
        <span className="flex items-center gap-[4px]">
          <Input
            className="tnum w-[132px]"
            type="date"
            value={dateStart}
            max={dateEnd || undefined}
            aria-label="起始日期"
            onChange={(e) => {
              setDateStart(e.target.value)
              setPage(1)
            }}
          />
          <span className="text-xs text-muted-foreground">至</span>
          <Input
            className="tnum w-[132px]"
            type="date"
            value={dateEnd}
            min={dateStart || undefined}
            aria-label="结束日期"
            onChange={(e) => {
              setDateEnd(e.target.value)
              setPage(1)
            }}
          />
        </span>
        {hasFilter && (
          <Button quiet variant="ghost" icon="X" onClick={resetFilters}>
            清除筛选
          </Button>
        )}
        <span className="toolbar-spacer" />
        <span className="toolbar-note">
          {error ? (
            <span className="text-state-error">{(error as Error).message}</span>
          ) : data ? (
            `倒序 ${data.count} 条留痕`
          ) : (
            '读取审计留痕…'
          )}
        </span>
      </div>

      <Panel
        title="审计日志（F3）"
        icon="ScrollText"
        padded={false}
        meta={hasFilter ? '按条件筛选' : '全量倒序'}
      >
        <DataTable
          columns={[
            {
              key: 'created_at',
              title: '时间',
              width: 150,
              tnum: true,
              render: (r) => <span className="text-xs text-muted-foreground">{fmtDateTime(r.created_at)}</span>,
            },
            {
              key: 'actor',
              title: '操作人',
              width: 120,
              render: (r) => (
                <span className="block max-w-[96px] truncate" title={r.actor_name || '系统'}>
                  {r.actor_name || (r.actor ? `用户 #${r.actor}` : '系统')}
                </span>
              ),
            },
            {
              key: 'ip',
              title: '来源',
              width: 150,
              secondary: true,
              render: (r) => (
                <span
                  className="mono block max-w-[126px] truncate text-xs text-muted-foreground"
                  title={[r.ip, r.user_agent].filter(Boolean).join('\n')}
                >
                  {r.ip || '—'}
                </span>
              ),
            },
            {
              key: 'action',
              title: '动作',
              width: 180,
              render: (r) => <AuditActionCell action={r.action} display={r.action_display} />,
            },
            {
              key: 'target',
              title: '对象',
              width: 220,
              mono: true,
              render: (r) => (
                <span className="block max-w-[196px] truncate text-xs" title={r.target}>
                  {r.target || '—'}
                </span>
              ),
            },
            {
              key: 'detail',
              title: '详情',
              render: (r) => (
                <div className="flex items-start gap-[8px]">
                  <div className="min-w-0 flex-1">
                    <AuditDetailCell row={r} />
                  </div>
                  {canViewBody(r, isAdmin) && (
                    <button
                      type="button"
                      className="flex flex-none items-center gap-[2px] whitespace-nowrap text-xs text-primary hover:underline"
                      onClick={() => setBodyRow(r)}
                    >
                      <Icon name="FileText" size={12} />
                      查看全文
                    </button>
                  )}
                </div>
              ),
            },
          ]}
          rows={data?.results ?? []}
          rowKey={(r) => r.id}
          loading={isFetching && !data}
          empty={
            <EmptyState
              icon="ScrollText"
              title={hasFilter ? '没有符合条件的审计记录' : '暂无审计留痕'}
              description={
                hasFilter
                  ? '放宽动作或时间区间，或改用对象关键词（如 智能体:名称、工具来源:名称、LLM配置）'
                  : '模型接入变更、出网开关切换、工具来源与智能体的写操作都会在此留痕'
              }
              action={
                hasFilter ? (
                  <Button variant="ghost" icon="X" onClick={resetFilters}>
                    清除筛选
                  </Button>
                ) : undefined
              }
            />
          }
        />
        <div className="flex items-center justify-between px-[12px]">
          {isFetching ? <span className="text-xs text-muted-foreground">读取中…</span> : <span />}
          {data && (
            <Pagination
              page={page}
              pageSize={pageSize}
              count={data.count}
              totalPages={Math.max(1, Math.ceil(data.count / pageSize))}
              onPageChange={setPage}
              onPageSizeChange={(v) => {
                setPageSize(v)
                setPage(1)
              }}
            />
          )}
        </div>
      </Panel>

      <div className="flex items-start gap-[6px] px-[2px] text-xs leading-[1.6] text-muted-foreground">
        <Icon name="Info" size={13} className="mt-[2px] flex-none" />
        <span>
          仅平台管理员可见，按时间倒序。动作共 {Object.keys(AUDIT_ACTION_LABELS).length} 类，与后端 AuditLog 取值一一对应；
          时间区间按天闭区间过滤（含首尾两日）。
        </span>
      </div>

      <ToolBodyDrawer row={bodyRow} onClose={() => setBodyRow(null)} />
    </>
  )
}
