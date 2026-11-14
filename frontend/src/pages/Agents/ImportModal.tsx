import { useEffect, useRef, useState } from 'react'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/EmptyState'
import { Input, Textarea } from '@/components/ui/Field'
import { DataTable } from '@/components/ui/DataTable'
import { ConfirmDialog, Modal } from '@/components/ui/Overlay'
import { SectionLabel } from '@/components/ui/Panel'
import { Seg } from '@/components/ui/Seg'
import {
  useCreateImportRequest,
  useImportRequests,
  usePreviewImport,
  useReviewImportRequest,
} from '@/api/hooks/useAgents'
import { ApiError } from '@/api/request'
import { toast } from '@/store/toast'
import { useUserStore } from '@/store/user'
import { fmtDateTime, fmtRelative } from '@/utils/format'
import type {
  AgentExportPackage,
  AgentImportIssue,
  AgentImportPreview,
  AgentImportRequest,
  AgentImportStatus,
} from '@/types/agent'

interface Props {
  open: boolean
  onClose: () => void
}

type Tab = 'submit' | 'records'

const STATUS_TONE: Record<AgentImportStatus, 'warning' | 'success' | 'error'> = {
  pending: 'warning',
  approved: 'success',
  rejected: 'error',
}

const STATUS_LABEL: Record<AgentImportStatus, string> = {
  pending: '待审批',
  approved: '已通过',
  rejected: '已驳回',
}

/** 依赖检查项：阻断红色、告警黄色，逐条给可读原因 */
function IssueLine({ issue }: { issue: AgentImportIssue }) {
  return (
    <div className="flex items-start gap-[7px] py-[3px]">
      <Badge tone={issue.level === 'blocking' ? 'error' : 'warning'}>
        {issue.level === 'blocking' ? '阻断' : '告警'}
      </Badge>
      <span className="min-w-0 text-xs leading-[1.6]">{issue.message}</span>
    </div>
  )
}

/** 跨环境导入：提交申请 + 查看审批状态（单级审批，提交不生效） */
export function AgentImportModal({ open, onClose }: Props) {
  const isAdmin = useUserStore((s) => s.user?.is_staff ?? false)
  const meId = useUserStore((s) => s.user?.id ?? null)

  const [tab, setTab] = useState<Tab>('submit')
  const [packageData, setPackageData] = useState<AgentExportPackage | null>(null)
  const [fileName, setFileName] = useState('')
  const [pasteText, setPasteText] = useState('')
  const [preview, setPreview] = useState<AgentImportPreview | null>(null)
  const [statusFilter, setStatusFilter] = useState('')
  const [review, setReview] = useState<{ request: AgentImportRequest; action: 'approve' | 'reject' } | null>(null)
  const [note, setNote] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  const previewImport = usePreviewImport()
  const createRequest = useCreateImportRequest()
  const reviewRequest = useReviewImportRequest()
  const { data: records, isFetching } = useImportRequests(
    { page: 1, page_size: 50, ...(statusFilter ? { status: statusFilter } : {}) },
    open && tab === 'records',
  )

  useEffect(() => {
    if (open) return
    // 关闭后清空提交态，避免下次打开残留上一个环境的导入包
    setTab('submit')
    setPackageData(null)
    setFileName('')
    setPasteText('')
    setPreview(null)
    setStatusFilter('')
    setReview(null)
    setNote('')
  }, [open])

  async function pickFile(file: File) {
    try {
      applyPackage(JSON.parse(await file.text()), file.name)
      setPasteText('')
    } catch (err) {
      resetPackage()
      toast.error(err instanceof Error ? err.message : '文件解析失败')
    } finally {
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  /** 校验导入包结构后触发预览；文件与粘贴共用同一条路径 */
  function applyPackage(parsed: AgentExportPackage, label: string) {
    if (!parsed || typeof parsed !== 'object' || !parsed.agent || typeof parsed.agent !== 'object') {
      throw new Error('格式不正确：缺少 agent 配置')
    }
    setPackageData(parsed)
    setFileName(label)
    setPreview(null)
    previewImport.mutate(parsed, {
      onSuccess: (data) => setPreview(data),
      onError: (err) => toast.error((err as Error).message),
    })
  }

  function resetPackage() {
    setPackageData(null)
    setFileName('')
    setPreview(null)
  }

  function submit() {
    if (!packageData) return
    createRequest.mutate(packageData, {
      onSuccess: (res) => {
        toast.success(res.detail)
        setPackageData(null)
        setFileName('')
        setPasteText('')
        setPreview(null)
        setTab('records')
      },
      onError: (err) => {
        const body = err instanceof ApiError ? (err.body as { blocking?: AgentImportIssue[] } | null) : null
        if (body?.blocking?.length) setPreview((prev) => (prev ? { ...prev, blocking: body.blocking ?? [] } : prev))
        toast.error((err as Error).message)
      },
    })
  }

  function confirmReview() {
    if (!review) return
    reviewRequest.mutate(
      { id: review.request.id, action: review.action, note: note.trim() || undefined },
      {
        onSuccess: (res) => {
          toast.success(res.detail)
          setReview(null)
          setNote('')
        },
        onError: (err) => toast.error((err as Error).message),
      },
    )
  }

  const counts = preview?.summary.counts
  const canSubmit = Boolean(packageData) && Boolean(preview) && (preview?.blocking.length ?? 1) === 0

  const footer = (
    <>
      <span className="mr-auto text-xs text-muted-foreground">
        审批人必须是平台管理员，且不能是提交人本人
      </span>
      <Button variant="ghost" onClick={onClose}>
        关闭
      </Button>
      {tab === 'submit' && (
        <Button
          variant="primary"
          icon="Send"
          disabled={!canSubmit}
          loading={createRequest.isPending}
          onClick={submit}
        >
          提交导入申请
        </Button>
      )}
    </>
  )

  return (
    <>
      <Modal open={open} title="导入智能体（单级审批）" icon="Upload" wide onClose={onClose} footer={footer}>
        <div className="mb-[10px] flex items-center gap-[10px]">
          <Seg
            items={[
              { value: 'submit', label: '提交导入' },
              { value: 'records', label: '导入申请' },
            ]}
            value={tab}
            onChange={(v) => setTab(v as Tab)}
          />
          <span className="toolbar-note">
            提交 ≠ 生效：需另一名平台管理员审批通过后才创建草稿，重新发布才上线
          </span>
        </div>

        {tab === 'submit' ? (
          <div className="grid gap-[12px]">
            <div>
              <SectionLabel>导入包</SectionLabel>
              <div className="mt-[6px] flex items-center gap-[8px]">
                <input
                  ref={fileRef}
                  type="file"
                  accept="application/json,.json"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0]
                    if (file) void pickFile(file)
                  }}
                />
                <Button variant="ghost" icon="Upload" disabled={previewImport.isPending} onClick={() => fileRef.current?.click()}>
                  选择导出的 JSON 文件
                </Button>
                <span className="text-xs text-muted-foreground">
                  {fileName || '从源环境「导出」得到的 .json 文件'}
                </span>
              </div>
              <p className="mt-[5px] text-xs text-muted-foreground">
                导出包不含任何密钥；模型与工具来源密钥需在本环境重新填写。来源/技能按标识映射，知识库与部门按名称映射。
              </p>
              <div className="mt-[8px]">
                <Textarea
                  rows={4}
                  className="mono text-xs"
                  value={pasteText}
                  placeholder="也可以直接粘贴导出包 JSON（便于从聊天或邮件里转过来）"
                  onChange={(e) => setPasteText(e.target.value)}
                />
                <div className="mt-[6px] flex items-center justify-end gap-[8px]">
                  <span className="mr-auto text-xs text-muted-foreground">
                    文件与粘贴二选一，都会先做依赖预览
                  </span>
                  <Button
                    variant="ghost"
                    quiet
                    disabled={!pasteText.trim()}
                    onClick={() => {
                      try {
                        applyPackage(JSON.parse(pasteText), '粘贴的 JSON')
                      } catch (err) {
                        resetPackage()
                        toast.error(err instanceof Error ? err.message : 'JSON 解析失败')
                      }
                    }}
                  >
                    解析并预览
                  </Button>
                </div>
              </div>
            </div>

            {preview ? (
              <div className="grid gap-[10px]">
                <div className="panel px-[12px] py-[10px]">
                  <div className="flex items-start justify-between gap-[10px]">
                    <div className="min-w-0">
                      <div className="row-title">{preview.summary.agent_name || '未命名'}</div>
                      <div className="row-sub">{preview.summary.description || '无用途描述'}</div>
                    </div>
                    {preview.summary.source?.agent && (
                      <span className="text-xs text-muted-foreground">
                        来源：{preview.summary.source.agent}
                        {preview.summary.source.current_version
                          ? ` v${preview.summary.source.current_version}`
                          : ''}
                      </span>
                    )}
                  </div>
                  <div className="mt-[8px] flex flex-wrap gap-[6px] text-xs text-muted-foreground">
                    {preview.summary.model_name ? (
                      <span className="mono">{preview.summary.model_name}</span>
                    ) : (
                      <span>未配置模型</span>
                    )}
                    <span>· 工具来源 {counts?.mcp_servers ?? 0}</span>
                    <span>· 技能 {counts?.skills ?? 0}</span>
                    <span>· 知识库 {counts?.knowledge_bases ?? 0}</span>
                    <span>· 可见部门 {counts?.departments ?? 0}</span>
                  </div>
                </div>

                {preview.blocking.length > 0 && (
                  <div className="panel px-[12px] py-[10px]">
                    <SectionLabel>阻断项（必须修正才能提交）</SectionLabel>
                    <div className="mt-[5px]">
                      {preview.blocking.map((issue, i) => (
                        <IssueLine key={`${issue.code}-${i}`} issue={issue} />
                      ))}
                    </div>
                  </div>
                )}

                {preview.warnings.length > 0 && (
                  <div className="panel px-[12px] py-[10px]">
                    <SectionLabel>依赖告警（可继续，但对应能力会被跳过或降级）</SectionLabel>
                    <div className="mt-[5px]">
                      {preview.warnings.map((issue, i) => (
                        <IssueLine key={`${issue.code}-${i}`} issue={issue} />
                      ))}
                    </div>
                  </div>
                )}

                {!preview.blocking.length && !preview.warnings.length && (
                  <div className="text-xs text-state-success">依赖全部可在本环境映射，可直接提交审批。</div>
                )}
              </div>
            ) : (
              <EmptyState
                icon="FileJson"
                title="尚未选择导入包"
                description="选择从另一环境导出的智能体 JSON，预览依赖映射与阻断项后再提交。"
              />
            )}
          </div>
        ) : (
          <div>
            <div className="mb-[8px] flex items-center gap-[10px]">
              <Seg
                items={[
                  { value: '', label: '全部' },
                  { value: 'pending', label: '待审批' },
                  { value: 'approved', label: '已通过' },
                  { value: 'rejected', label: '已驳回' },
                ]}
                value={statusFilter}
                onChange={setStatusFilter}
              />
              <span className="toolbar-note">
                {isAdmin ? '管理员视图：可审批他人提交的申请' : '仅显示你提交的导入申请'}
              </span>
            </div>
            <DataTable
              columns={[
                {
                  key: 'name',
                  title: '导入名称',
                  render: (r) => (
                    <span className="row-main">
                      <span className="row-title">{r.agent_name}</span>
                      <span className="row-sub truncate">{r.description || '无用途描述'}</span>
                    </span>
                  ),
                },
                {
                  key: 'status',
                  title: '审批状态',
                  width: 96,
                  render: (r) => (
                    <Badge tone={STATUS_TONE[r.status]} dot={r.status === 'pending'}>
                      {r.status_display || STATUS_LABEL[r.status]}
                    </Badge>
                  ),
                },
                {
                  key: 'submitted',
                  title: '提交',
                  width: 150,
                  render: (r) => (
                    <span className="text-xs text-muted-foreground">
                      {r.submitted_by_name || '—'}
                      <span className="block">{fmtRelative(r.submitted_at)}</span>
                    </span>
                  ),
                },
                {
                  key: 'reviewed',
                  title: '审批',
                  width: 170,
                  render: (r) => (
                    <span className="text-xs text-muted-foreground">
                      {r.reviewed_by_name || '—'}
                      {r.reviewed_at && <span className="block">{fmtDateTime(r.reviewed_at)}</span>}
                      {r.review_note && <span className="block">意见：{r.review_note}</span>}
                    </span>
                  ),
                },
                {
                  key: 'deps',
                  title: '依赖告警',
                  width: 82,
                  render: (r) =>
                    r.warnings?.length ? (
                      <Badge tone="warning" title={r.warnings.map((w) => w.message).join('\n')}>
                        {r.warnings.length} 项
                      </Badge>
                    ) : (
                      <span className="text-xs text-muted-foreground">无</span>
                    ),
                },
                {
                  key: 'result',
                  title: '结果',
                  width: 120,
                  render: (r) =>
                    r.result?.agent_name ? (
                      <span className="text-xs">
                        已建草稿
                        <span className="mono block">{r.result.agent_slug}</span>
                      </span>
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    ),
                },
                {
                  key: 'ops',
                  title: '操作',
                  width: 116,
                  align: 'right',
                  render: (r) => {
                    const canReview = isAdmin && r.status === 'pending' && r.submitted_by !== meId
                    if (!canReview) {
                      return <span className="text-xs text-muted-foreground">—</span>
                    }
                    return (
                      <span className="flex items-center justify-end gap-[4px]">
                        <Button
                          variant="ghost"
                          onClick={() => {
                            setReview({ request: r, action: 'approve' })
                            setNote('')
                          }}
                        >
                          通过
                        </Button>
                        <Button
                          variant="ghost"
                          onClick={() => {
                            setReview({ request: r, action: 'reject' })
                            setNote('')
                          }}
                        >
                          驳回
                        </Button>
                      </span>
                    )
                  },
                },
              ]}
              rows={records?.results ?? []}
              rowKey={(r) => r.id}
              loading={isFetching && !records}
              empty={
                <EmptyState
                  compact
                  icon="Inbox"
                  title="暂无导入申请"
                  description="上传智能体 JSON 后提交，管理员在这里完成单级审批"
                />
              }
            />
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={!!review}
        title={
          review?.action === 'approve'
            ? `通过导入「${review?.request.agent_name ?? ''}」`
            : `驳回导入「${review?.request.agent_name ?? ''}」`
        }
        tone={review?.action === 'reject' ? 'danger' : 'default'}
        message={
          review?.action === 'approve'
            ? '通过后会在本环境创建该智能体的草稿（不自动发布），需再执行发布检查与发布才会上线；依赖缺失项会被跳过并留告警。'
            : '驳回后不创建任何配置，提交人可在修正导入包后重新提交。'
        }
        confirmText={review?.action === 'approve' ? '确认通过' : '确认驳回'}
        loading={reviewRequest.isPending}
        onConfirm={confirmReview}
        onCancel={() => {
          setReview(null)
          setNote('')
        }}
      >
        <div className="mt-[8px]">
          <Input
            value={note}
            maxLength={200}
            placeholder={review?.action === 'approve' ? '审批意见（可选）' : '驳回理由（可选）'}
            onChange={(e) => setNote(e.target.value)}
          />
        </div>
      </ConfirmDialog>
    </>
  )
}
