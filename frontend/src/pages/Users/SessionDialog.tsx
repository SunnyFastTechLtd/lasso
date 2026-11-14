import { useEffect, useState } from 'react'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Icon } from '@/components/ui/Icon'
import { Drawer } from '@/components/ui/Overlay'
import { Panel } from '@/components/ui/Panel'
import {
  useRevokeUserSessions,
  useUserSessions,
  type AdminUser,
} from '@/api/users'
import { toast } from '@/store/toast'
import { fmtDateTime, fmtRelative } from '@/utils/format'
import { readableError } from '@/utils/password'

interface Props {
  /** null 表示关闭 */
  target: AdminUser | null
  /** 当前登录管理员在操作自己 */
  isSelf: boolean
  onClose: () => void
}

function sourceLabel(action: string | null): string {
  if (action === 'login_success') return '登录签发'
  if (action === 'password_change') return '改密后重签'
  return '来源未识别'
}

export function SessionDialog({ target, isSelf, onClose }: Props) {
  const { data, isLoading, isFetching, refetch } = useUserSessions(target?.id ?? null)
  const revoke = useRevokeUserSessions()
  const [confirming, setConfirming] = useState(false)

  useEffect(() => {
    setConfirming(false)
  }, [target?.id])

  async function confirmRevoke() {
    if (!target) return
    try {
      const result = await revoke.mutateAsync(target.id)
      toast.success(
        `已强制下线「${target.username}」：吊销 ${result.revoked} 个会话令牌；账号仍可立即重新登录`,
      )
      setConfirming(false)
      void refetch()
    } catch (err) {
      toast.error(readableError(err))
    }
  }

  return (
    <Drawer
      open={target !== null}
      title={target ? `会话 · ${target.nickname || target.username}` : '会话'}
      meta="查看该账号当前有几处登录；强制下线与停用账号是两个独立动作"
      onClose={revoke.isPending ? () => undefined : onClose}
      footer={
        confirming ? (
          <>
            <Button variant="ghost" onClick={() => setConfirming(false)} disabled={revoke.isPending}>
              取消
            </Button>
            <Button variant="danger" icon="LogOut" loading={revoke.isPending} onClick={confirmRevoke}>
              确认强制下线
            </Button>
          </>
        ) : (
          <>
            <Button variant="ghost" onClick={onClose}>
              关闭
            </Button>
            <Button
              variant="danger"
              icon="LogOut"
              disabled={!data || data.count === 0}
              onClick={() => setConfirming(true)}
            >
              强制下线
            </Button>
          </>
        )
      }
    >
      <div className="flex flex-col gap-[10px]">
        {confirming && target && (
          <div className="rounded-md border border-state-error-border bg-state-error-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-error">
            <div className="font-medium">将立即吊销「{target.username}」全部登录令牌</div>
            <ul className="mt-[4px] list-disc pl-[16px]">
              <li>对方下一次刷新 token 立即返回 401，需要重新登录；</li>
              <li>已签发的 access token 最长 60 分钟内仍可访问，到期即失效；</li>
              <li>账号不会被停用：对方仍可立即重新登录。若需阻止登录，请单独使用「停用账号」。</li>
              {isSelf && <li>你正在对本人操作：当前页面也会在 access 到期后退出登录。</li>}
            </ul>
          </div>
        )}

        <div className="flex items-start gap-[6px] rounded-md border border-border bg-surface-2 px-[10px] py-[8px] text-xs leading-relaxed text-muted-foreground">
          <Icon name="Info" size={13} className="mt-[1px] flex-none text-primary" />
          <span>
            {data ? `当前共 ${data.count} 处登录。` : isLoading ? '读取中…' : '读取失败'}
            {data?.last_used_note}
          </span>
        </div>

        {data && data.count > 0 && (
          <Panel padded={false}>
            <div className="divide-y divide-border">
              {data.results.map((session, index) => (
                <div key={session.id} className="px-[12px] py-[10px]">
                  <div className="flex flex-wrap items-center gap-[6px]">
                    <span className="row-title">登录 #{index + 1}</span>
                    <Badge tone={session.source_action ? 'primary' : 'neutral'}>
                      {sourceLabel(session.source_action)}
                    </Badge>
                    <span className="ml-auto text-xs text-muted-foreground">
                      签发于 {fmtDateTime(session.created_at)}（{fmtRelative(session.created_at)}）
                    </span>
                  </div>
                  <div className="mt-[6px] grid grid-cols-2 gap-x-[12px] gap-y-[4px] text-xs text-muted-foreground">
                    <span>
                      过期时间：<span className="text-foreground">{fmtDateTime(session.expires_at)}</span>
                    </span>
                    <span>
                      最后使用：<span className="text-foreground">未记录</span>
                    </span>
                    <span>
                      IP：<span className="text-foreground mono">{session.ip || '未关联'}</span>
                    </span>
                    <span className="truncate" title={session.user_agent}>
                      UA：<span className="text-foreground">{session.user_agent || '未关联'}</span>
                    </span>
                  </div>
                  <div className="mt-[4px] text-xs text-muted-foreground">{session.source_note}</div>
                </div>
              ))}
            </div>
          </Panel>
        )}

        {data && data.count === 0 && (
          <div className="rounded-md border border-border bg-surface-2 px-[10px] py-[10px] text-xs leading-relaxed text-muted-foreground">
            该账号当前没有活跃登录。已登出、已被强制下线或令牌已过期的登录不会列在这里。
          </div>
        )}

        {isFetching && !isLoading && (
          <span className="text-xs text-muted-foreground">正在刷新会话列表…</span>
        )}
        {data && <div className="text-xs text-muted-foreground">{data.correlation_note}</div>}
      </div>
    </Drawer>
  )
}
