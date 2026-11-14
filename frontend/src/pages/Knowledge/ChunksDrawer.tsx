import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/EmptyState'
import { Drawer } from '@/components/ui/Overlay'
import { SectionLabel } from '@/components/ui/Panel'
import { Icon } from '@/components/ui/Icon'
import { useDocumentChunks } from '@/api/hooks/useKnowledge'
import { fmtDateTime } from '@/utils/format'
import type { KnowledgeDocument } from '@/api/knowledge'

interface Props {
  doc: KnowledgeDocument | null
  onClose: () => void
}

/** 切片只读预览：核对分块质量与向量化结果，禁检/重建由后端管理端另行处理 */
export function ChunksDrawer({ doc, onClose }: Props) {
  const { data: chunks, isPending, error } = useDocumentChunks(doc?.id ?? null)
  const list = chunks ?? []

  return (
    <Drawer
      open={doc !== null}
      onClose={onClose}
      title={doc ? `切片预览 · ${doc.title}` : '切片预览'}
      meta={doc ? `${doc.file_name} · ${doc.size_human} · ${doc.chunk_count} 个切片 · 只读` : undefined}
      footer={
        <Button variant="ghost" onClick={onClose}>
          关闭
        </Button>
      }
    >
      {doc && (
        <div className="col-stack">
          <div className="flex items-start gap-[6px] rounded-md border border-border bg-surface-sunken px-[10px] py-[8px] text-xs leading-[1.5] text-muted-foreground">
            <Icon name="Info" size={12} className="mt-[2px] flex-none" />
            <span>
              检索命中的是这个切片列表中的向量。此处仅展示分块内容与向量化状态，不提供在线问答试用。
            </span>
          </div>

          {isPending && (
            <div className="panel-blank">
              <Icon name="LoaderCircle" size={14} className="animate-spin" />
              读取切片…
            </div>
          )}

          {error && <EmptyState compact icon="TriangleAlert" title="切片读取失败" description={(error as Error).message} />}

          {!isPending && !error && list.length === 0 && (
            <EmptyState
              compact
              icon="Layers"
              title="暂无切片"
              description={doc?.status === 'failed' ? '该资料解析失败，重新处理后会生成切片' : '资料尚未处理完成，处理完成后自动生成切片'}
            />
          )}

          {!isPending &&
            !error &&
            list.map((c) => (
              <div key={c.id} className="rounded-md border border-border px-[10px] py-[8px]">
                <div className="flex flex-wrap items-center gap-[6px]">
                  <SectionLabel className="tnum">切片 #{c.chunk_index}</SectionLabel>
                  <Badge tone={c.has_embedding ? 'success' : 'warning'} dot>
                    {c.has_embedding ? '已向量化' : '无向量'}
                  </Badge>
                  <Badge tone={c.is_active ? 'neutral' : 'error'}>{c.is_active ? '可检索' : '已禁检'}</Badge>
                  {c.meta?.page ? (
                    <Badge tone="neutral">第 {c.meta.page} 页</Badge>
                  ) : null}
                  {c.meta?.ocr ? (
                    <Badge tone="warning">OCR · 可能有误差</Badge>
                  ) : null}
                  <span className="ml-auto text-xs text-muted-foreground">{fmtDateTime(c.created_at)}</span>
                </div>
                <p className="mt-[6px] max-h-[180px] overflow-auto whitespace-pre-wrap break-words text-xs leading-[1.6] text-sidebar-foreground">
                  {c.content}
                </p>
              </div>
            ))}
        </div>
      )}
    </Drawer>
  )
}
