import { Badge, type BadgeTone } from '@/components/ui/Badge'
import {
  KB_SCOPE_LABEL, type DocStatus, type ExtractionReport, type KbScope, type KnowledgeBase,
} from '@/api/knowledge'

export { extractFieldErrors } from '@/pages/Mcp/shared'

const SCOPE_TONE: Record<KbScope, BadgeTone> = {
  public: 'primary',
  department: 'success',
  private: 'neutral',
}

/** 可见范围徽标：库的可见范围决定谁能检索到（PRD E2 越权边界的第一道说明） */
export function ScopeBadge({ kb }: { kb: KnowledgeBase }) {
  return (
    <Badge tone={SCOPE_TONE[kb.scope] ?? 'neutral'} title={kb.scope === 'department' && kb.department ? `共享部门：${kb.department}` : undefined}>
      {KB_SCOPE_LABEL[kb.scope] ?? kb.scope}
    </Badge>
  )
}

const DOC_TONE: Record<DocStatus, BadgeTone> = {
  pending: 'neutral',
  processing: 'warning',
  completed: 'success',
  failed: 'error',
}

const DOC_LABEL: Record<DocStatus, string> = {
  pending: '待处理',
  processing: '处理中',
  completed: '已完成',
  failed: '失败',
}

export function DocStatusBadge({ status, error }: { status: DocStatus; error?: string }) {
  return (
    <Badge tone={DOC_TONE[status] ?? 'neutral'} dot title={error || undefined}>
      {DOC_LABEL[status] ?? status}
    </Badge>
  )
}

const PAGE_REASON: Record<string, string> = {
  ocr_disabled: '未开启 OCR',
  tesseract_not_installed: '未安装 tesseract',
  tesseract_empty: 'OCR 未识别出文字',
  tesseract_timeout: 'OCR 超时',
  max_pages_exceeded: '超过单文档页数上限',
}

/** 提取报告汇总：只出徽标，逐页明细点开再看（避免表格被日志淹没） */
export function ExtractionSummary({ report }: { report?: ExtractionReport }) {
  const ocrPages = report?.ocr_pages ?? []
  const failed = report?.failed_pages ?? []
  if (!ocrPages.length && !failed.length) return null

  return (
    <span className="flex min-w-0 flex-wrap items-center gap-[4px]" onClick={(e) => e.stopPropagation()}>
      {ocrPages.length > 0 && (
        <Badge tone="warning" title={`OCR 页：${ocrPages.join('、')}`}>
          OCR {ocrPages.length} 页
        </Badge>
      )}
      {failed.length > 0 && (
        <Badge
          tone="error"
          title={`未解析页：${failed.map((p) => p.page).join('、')}`}
        >
          未解析 {failed.length} 页
        </Badge>
      )}
      <details className="relative min-w-0">
        <summary className="cursor-pointer text-xs text-muted-foreground">逐页</summary>
        <div className="absolute left-0 z-20 mt-[4px] max-h-[220px] w-[280px] overflow-auto rounded-md border border-border bg-surface px-[8px] py-[6px] shadow-md">
          {(report?.pages ?? []).map((page) => (
            <div
              key={`${page.page}`}
              className="flex items-center justify-between gap-[8px] border-b border-border py-[3px] text-xs last:border-0"
            >
              <span className="text-foreground">
                {page.page ? `第 ${page.page} 页` : '正文'}
                <span className="ml-[4px] text-muted-foreground">
                  {page.ocr ? `OCR · ${page.engine || '—'}` : page.status === 'failed' ? '未解析' : '文字层'}
                </span>
              </span>
              <span className="tnum text-muted-foreground">
                {page.status === 'failed'
                  ? PAGE_REASON[page.reason] || page.reason || '失败'
                  : `${page.chars} 字`}
              </span>
            </div>
          ))}
          {(report?.pages ?? []).length === 0 && (
            <div className="py-[4px] text-xs text-muted-foreground">无逐页信息</div>
          )}
        </div>
      </details>
    </span>
  )
}
