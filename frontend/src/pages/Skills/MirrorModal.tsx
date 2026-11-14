import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Icon } from '@/components/ui/Icon'
import { Modal } from '@/components/ui/Overlay'
import { useMirrorState, useRebuildMirror } from '@/api/hooks/useSkills'
import { toast } from '@/store/toast'

const STATUS_LABEL: Record<string, string> = {
  active: '已启用',
  builtin: '内置',
  disabled: '已停用',
  pending: '待审核',
  invalid: '校验未通过',
}

/** 运行时镜像可见性（D4）：目录内容 = 装配面 ∩ 运行时面；全量重建仅管理员 */
export function MirrorModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { data: state } = useMirrorState(open)
  const rebuild = useRebuildMirror()

  async function runRebuild() {
    try {
      const res = await rebuild.mutateAsync()
      toast.success(`镜像已重建：写入 ${res.mirrored} 项，清理 ${res.removed} 项`)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  return (
    <Modal
      open={open}
      title="运行时技能镜像"
      onClose={onClose}
      wide
      footer={
        <>
          <span className="text-xs text-muted-foreground">镜像与台账不一致时才需重建</span>
          <Button variant="ghost" onClick={onClose}>
            关闭
          </Button>
          <Button variant="primary" icon="RefreshCw" loading={rebuild.isPending} onClick={runRebuild}>
            全量重建
          </Button>
        </>
      }
    >
      {!state ? (
        <div className="flex items-center gap-[6px] py-[16px] text-xs text-muted-foreground">
          <Icon name="LoaderCircle" size={14} className="animate-spin" />
          读取镜像目录…
        </div>
      ) : (
        <div className="col-stack gap-[8px]">
          <div className="text-xs text-muted-foreground">
            镜像根目录：<span className="mono text-sidebar-foreground break-all">{state.root}</span>
          </div>
          {state.mirrored.length === 0 ? (
            <div className="text-xs text-muted-foreground">当前没有进入运行时的技能（需先审核启用）。</div>
          ) : (
            <div className="col-stack gap-[2px]">
              {state.mirrored.map((m) => (
                <div key={m.slug} className="list-row">
                  <span className="row-main min-w-0">
                    <span className="row-title truncate">{m.name}</span>
                    <span className="row-sub mono truncate">{m.path}</span>
                  </span>
                  <Badge tone={m.status === 'builtin' ? 'primary' : m.status === 'active' ? 'success' : 'neutral'}>
                    {STATUS_LABEL[m.status] ?? m.status}
                  </Badge>
                  <span className="tnum w-[70px] text-right text-xs text-muted-foreground">{m.runtime_chars} 字</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}
