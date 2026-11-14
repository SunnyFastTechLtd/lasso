import { useState } from 'react'
import { Badge } from '@/components/ui/Badge'
import { Button, IconButton } from '@/components/ui/Button'
import { Input } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { ConfirmDialog } from '@/components/ui/Overlay'
import { Panel } from '@/components/ui/Panel'
import { useDeleteSkillFile, useUploadSkillFiles } from '@/api/hooks/useSkills'
import { skillsApi } from '@/api/skills'
import { toast } from '@/store/toast'
import { fmtBytes, fmtRelative } from '@/utils/format'
import type { Skill, SkillFileMeta } from '@/types/skill'
import { extractRiskChallenge, type RiskChallenge } from './shared'
import { RiskConfirmModal } from './RiskConfirmModal'

interface Props {
  skill: Skill
  /** 内置技能不可改附件；非管理员只读 */
  editable: boolean
}

/** C1 技能包附件：批量上传、清单、下载、删除；高危走 422 确认后放行 */
export function SkillFiles({ skill, editable }: Props) {
  const [subdir, setSubdir] = useState('')
  const [picked, setPicked] = useState<File[]>([])
  const [challenge, setChallenge] = useState<RiskChallenge | null>(null)
  const [pendingDelete, setPendingDelete] = useState<SkillFileMeta | null>(null)

  const upload = useUploadSkillFiles()
  const removeFile = useDeleteSkillFile()

  const paths = picked.map((file) => {
    const folder = subdir.trim().replace(/^\/+|\/+$/g, '')
    return folder ? `${folder}/${file.name}` : file.name
  })

  async function runUpload(ack?: { risk_ack: true; risk_fingerprint: string }) {
    try {
      await upload.mutateAsync({
        id: skill.id,
        payload: { files: picked, paths, ...ack },
      })
      toast.success(`已上传 ${picked.length} 个附件；审核通过后随新版本进入运行时`)
      setPicked([])
      setChallenge(null)
    } catch (err) {
      const next = extractRiskChallenge(err)
      if (next && !ack) {
        setChallenge(next)
        return
      }
      toast.error((err as Error).message)
      setChallenge(null)
    }
  }

  async function runDelete() {
    if (!pendingDelete) return
    try {
      await removeFile.mutateAsync({ id: skill.id, fileId: pendingDelete.id })
      toast.success(`已删除附件 ${pendingDelete.path}`)
      setPendingDelete(null)
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  return (
    <>
      <Panel
        title="技能包附件（C1）"
        icon="FolderOpen"
        meta={`${skill.file_count} 个 · ${fmtBytes(skill.package_bytes)}`}
        padded
      >
        {skill.files.length === 0 ? (
          <div className="field-hint">暂无附件。技能主文件是 SKILL.md；参考文档、示例等以附件形式随技能包分发。</div>
        ) : (
          <div className="col-stack gap-[4px]">
            {skill.files.map((file) => (
              <div key={file.id} className="list-row">
                <span className="row-main min-w-0">
                  <span className="row-title mono truncate">{file.path}</span>
                  <span className="row-sub text-xs text-muted-foreground">
                    {fmtBytes(file.size)} · 更新于 {fmtRelative(file.updated_at)}
                  </span>
                </span>
                <IconButton
                  icon="Download"
                  label="下载"
                  onClick={async () => {
                    try {
                      await skillsApi.downloadFile(file.download_url, file.path.split('/').pop() || file.path)
                    } catch (err) {
                      toast.error((err as Error).message)
                    }
                  }}
                />
                {editable && (
                  <IconButton icon="Trash2" label="删除" onClick={() => setPendingDelete(file)} />
                )}
              </div>
            ))}
          </div>
        )}

        {skill.file_scan_warnings?.length > 0 && (
          <div className="mt-[8px] col-stack gap-[4px]">
            {skill.file_scan_warnings.map((w, i) => (
              <div key={i} className="flex items-start gap-[4px] text-xs leading-relaxed text-state-warning">
                <Icon name="TriangleAlert" size={12} className="mt-[1px] flex-none" />
                {w}
              </div>
            ))}
          </div>
        )}

        {editable && (
          <div className="mt-[10px] col-stack gap-[6px] border-t pt-[10px]">
            <div className="text-xs text-muted-foreground">
              允许的类型由系统设置统一维护：仅文本文件，单个 ≤200KB，最多 20 个、整包 ≤2MB。
            </div>
            <div className="flex flex-wrap items-end gap-[8px]">
              <label className="min-w-0 flex-1">
                <span className="section-label">子目录（可留空，如 references）</span>
                <Input
                  className="mt-[4px]"
                  value={subdir}
                  placeholder="references"
                  onChange={(e) => setSubdir(e.target.value)}
                />
              </label>
              <label className="min-w-0 flex-1">
                <span className="section-label">选择文件（可多选）</span>
                <input
                  type="file"
                  multiple
                  className="mt-[6px] block w-full text-xs"
                  onChange={(e) => setPicked(Array.from(e.target.files ?? []))}
                />
              </label>
              <Button
                variant="primary"
                icon="Upload"
                loading={upload.isPending}
                disabled={picked.length === 0}
                onClick={() => void runUpload()}
              >
                上传
              </Button>
            </div>
            {picked.length > 0 && (
              <div className="flex flex-wrap gap-[4px]">
                {paths.map((path) => (
                  <Badge key={path} tone="neutral">
                    {path}
                  </Badge>
                ))}
              </div>
            )}
          </div>
        )}
      </Panel>

      <RiskConfirmModal
        challenge={challenge}
        title="附件命中高危规则"
        busy={upload.isPending}
        onCancel={() => setChallenge(null)}
        onConfirm={() =>
          void runUpload({ risk_ack: true, risk_fingerprint: challenge?.fingerprint ?? '' })
        }
      />

      <ConfirmDialog
        open={pendingDelete !== null}
        title="删除附件"
        tone="danger"
        confirmText="删除"
        loading={removeFile.isPending}
        message={`确认删除 ${pendingDelete?.path ?? ''}？删除后需重新提交审核，运行时在新版本通过前不受影响。`}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => void runDelete()}
      />
    </>
  )
}
