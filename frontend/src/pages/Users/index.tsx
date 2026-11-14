import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PageContainer, PageHeader } from '@/components/AppShell'
import { Badge } from '@/components/ui/Badge'
import { Button, IconButton } from '@/components/ui/Button'
import { DataTable } from '@/components/ui/DataTable'
import { EmptyState } from '@/components/ui/EmptyState'
import { Field, Input } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { ConfirmDialog, Modal } from '@/components/ui/Overlay'
import { Pagination } from '@/components/ui/Pagination'
import { Panel } from '@/components/ui/Panel'
import { useAdminUsers, useCreateUser, useDeleteUser, useUpdateUser, type AdminUser, type AdminUserPayload } from '@/api/users'
import { useDepartments } from '@/api/departments'
import { authApi } from '@/api/auth'
import { useUserStore } from '@/store/user'
import { toast } from '@/store/toast'
import { readableError } from '@/utils/password'
import { fmtDateTime, fmtRelative } from '@/utils/format'
import { UserForm } from './UserForm'
import { ResetPasswordDialog } from './ResetPasswordDialog'
import { SessionDialog } from './SessionDialog'

export default function UsersPage() {
  const navigate = useNavigate()
  const me = useUserStore((s) => s.user)
  const setUser = useUserStore((s) => s.setUser)

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')

  const [formOpen, setFormOpen] = useState(false)
  const [formUser, setFormUser] = useState<AdminUser | null>(null)
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null)
  const [disableTarget, setDisableTarget] = useState<AdminUser | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<AdminUser | null>(null)
  const [deleteText, setDeleteText] = useState('')
  const [downgrade, setDowngrade] = useState<{ user: AdminUser; payload: AdminUserPayload } | null>(null)
  const [sessionTarget, setSessionTarget] = useState<AdminUser | null>(null)
  const [deletedView, setDeletedView] = useState(false)

  useEffect(() => {
    const t = window.setTimeout(() => {
      setSearch(searchInput.trim())
      setPage(1)
    }, 350)
    return () => window.clearTimeout(t)
  }, [searchInput])

  const { data, isFetching } = useAdminUsers({
    page,
    page_size: pageSize,
    ...(search ? { search } : {}),
    ...(deletedView ? { deleted: true } : {}),
  })
  const { data: departments = [] } = useDepartments(formOpen)
  const create = useCreateUser()
  const update = useUpdateUser()
  const del = useDeleteUser()

  async function refreshSelf() {
    try {
      setUser(await authApi.profile())
    } catch {
      // 401 由拦截器统一处理；这里不阻断已成功的写操作提示
    }
  }

  function openCreate() {
    setFormUser(null)
    setFormOpen(true)
  }

  function openEdit(user: AdminUser) {
    setFormUser(user)
    setFormOpen(true)
  }

  async function saveExisting(user: AdminUser, payload: AdminUserPayload, successText: string) {
    const updated = await update.mutateAsync({ id: user.id, payload })
    toast.success(successText)
    setFormOpen(false)
    // 改的是自己（资料/角色）时同步 store，降级后菜单与路由守卫立即生效
    if (updated.id === me?.id) await refreshSelf()
  }

  async function submitForm(payload: AdminUserPayload) {
    if (!formUser) {
      const created = await create.mutateAsync(payload)
      toast.success(`已创建账号「${created.username}」，请通过安全渠道交付初始密码`)
      setFormOpen(false)
      return
    }
    if (formUser.is_staff && payload.is_staff === false) {
      setDowngrade({ user: formUser, payload })
      return
    }
    await saveExisting(formUser, payload, `已更新「${formUser.username}」的资料`)
  }

  async function confirmDowngrade() {
    if (!downgrade) return
    try {
      await saveExisting(
        downgrade.user,
        downgrade.payload,
        `已将「${downgrade.user.username}」降为普通用户，其管理入口立即失效`,
      )
      setDowngrade(null)
    } catch (err) {
      toast.error(readableError(err))
    }
  }

  async function enableUser(user: AdminUser) {
    try {
      await update.mutateAsync({ id: user.id, payload: { is_active: true } })
      toast.success(`已启用「${user.username}」：现在可以正常登录`)
    } catch (err) {
      toast.error(readableError(err))
    }
  }

  async function confirmDisable() {
    if (!disableTarget) return
    try {
      await update.mutateAsync({ id: disableTarget.id, payload: { is_active: false } })
      toast.success(`已停用「${disableTarget.username}」：登录被拒绝，刷新令牌已全部吊销`)
      setDisableTarget(null)
    } catch (err) {
      toast.error(readableError(err))
    }
  }

  async function submitReset(password: string) {
    if (!resetTarget) return
    await update.mutateAsync({ id: resetTarget.id, payload: { password } })
    toast.success(`已重置「${resetTarget.username}」的密码：其它设备已下线`)
    setResetTarget(null)
  }

  async function confirmDelete() {
    if (!deleteTarget) return
    try {
      await del.mutateAsync(deleteTarget.id)
      toast.success(`已删除账号「${deleteTarget.username}」：已停用并保留历史记录，可在「已删除账号」中查看`)
      setDeleteTarget(null)
      setDeleteText('')
    } catch (err) {
      toast.error(readableError(err))
    }
  }

  const deleteMatches = deleteTarget !== null && deleteText.trim() === deleteTarget.username

  return (
    <PageContainer>
      <PageHeader
        title="用户管理"
        subtitle="账号、角色与启停状态；停用/删除前请确认影响范围"
        actions={
          <>
            <Button icon="Building2" onClick={() => navigate('/departments')}>
              部门管理
            </Button>
            {!deletedView && (
              <Button variant="primary" icon="UserPlus" onClick={openCreate}>
                新建账号
              </Button>
            )}
          </>
        }
      />

      <div className="mt-[14px] flex items-start gap-[6px] rounded-md border border-border bg-surface-2 px-[10px] py-[8px] text-xs leading-relaxed text-muted-foreground">
        <Icon name="Info" size={13} className="mt-[1px] flex-none text-primary" />
        <span>
          系统始终保留至少一个有效管理员：最后一个管理员不能降级、停用或删除。「删除」是软删除：账号停用并保留历史会话与审计用于追溯，可从「已删除账号」查看；「强制下线」只清当前登录，不等同于停用账号。
        </span>
      </div>

      <div className="page-toolbar">
        <div className="relative w-[280px]">
          <Icon
            name="Search"
            size={13}
            className="pointer-events-none absolute left-[9px] top-1/2 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            className="pl-[28px]"
            value={searchInput}
            placeholder="搜索用户名 / 昵称 / 邮箱 / 手机…"
            onChange={(e) => setSearchInput(e.target.value)}
          />
        </div>
        <div className="ml-auto flex items-center gap-[8px]">
          <span className="toolbar-note">
            {data ? `共 ${data.count} 个账号` : '读取中…'}
            {search ? ' · 已按关键词筛选' : ''}
          </span>
          <Button
            icon={deletedView ? 'ArrowLeft' : 'Archive'}
            onClick={() => {
              setDeletedView((v) => !v)
              setPage(1)
            }}
          >
            {deletedView ? '返回账号列表' : '已删除账号'}
          </Button>
        </div>
      </div>

      <Panel padded={false}>
        <DataTable
          columns={[
            {
              key: 'user',
              title: '账号',
              render: (u) => (
                <span className="row-main">
                  <span className="row-title flex items-center gap-[6px]">
                    {u.nickname || u.username}
                    {u.id === me?.id && <Badge tone="primary">本人</Badge>}
                    {u.is_superuser && <Badge tone="warning">超级管理员</Badge>}
                  </span>
                  <span className="row-sub truncate">
                    <span className="mono">@{u.username}</span>
                    {u.email ? ` · ${u.email}` : ''}
                  </span>
                  {u.is_deleted && (
                    <span className="row-sub truncate text-state-error">
                      已删除 {fmtDateTime(u.deleted_at)}
                      {u.deleted_by_name ? ` · 操作人 ${u.deleted_by_name}` : ''}
                    </span>
                  )}
                </span>
              ),
            },
            {
              key: 'role',
              title: '角色',
              width: 150,
              render: (u) => (
                <span className="flex flex-wrap items-center gap-[4px]">
                  <Badge tone={u.is_staff || u.is_superuser ? 'primary' : 'neutral'}>
                    {u.is_superuser ? '超级管理员' : u.is_staff ? '管理员' : '普通用户'}
                  </Badge>
                  {u.is_last_admin && (
                    <Badge tone="warning" title="降级/停用/删除会导致系统没有管理员">
                      唯一管理员
                    </Badge>
                  )}
                </span>
              ),
            },
            {
              key: 'status',
              title: '状态',
              width: 90,
              render: (u) => (
                u.is_deleted ? (
                  <Badge tone="error" dot>已删除</Badge>
                ) : (
                  <Badge tone={u.is_active ? 'success' : 'neutral'} dot>
                    {u.is_active ? '已启用' : '已停用'}
                  </Badge>
                )
              ),
            },
            {
              key: 'org',
              title: '部门 / 职位',
              width: 150,
              secondary: true,
              render: (u) => (
                <span className="row-main">
                  <span className="row-title">{u.department || '未分配'}</span>
                  <span className="row-sub truncate">{u.position || '—'}</span>
                </span>
              ),
            },
            {
              key: 'last_login',
              title: '最近登录',
              width: 110,
              secondary: true,
              render: (u) => (
                <span className="text-xs text-muted-foreground">
                  {u.last_login ? fmtRelative(u.last_login) : '从未登录'}
                </span>
              ),
            },
            {
              key: 'ops',
              title: '操作',
              width: 140,
              render: (u) => {
                if (u.is_deleted) {
                  return <span className="text-xs text-muted-foreground">仅供追溯</span>
                }
                const isSelf = u.id === me?.id
                const disableBlocked = isSelf || u.is_last_admin
                const deleteBlocked = isSelf || u.is_last_admin
                return (
                  <span className="flex items-center justify-end gap-[4px]" onClick={(e) => e.stopPropagation()}>
                    <IconButton icon="Pencil" label="编辑资料 / 角色" onClick={() => openEdit(u)} />
                    <IconButton
                      icon="MonitorSmartphone"
                      label="查看会话 / 强制下线（独立于停用）"
                      onClick={() => setSessionTarget(u)}
                    />
                    <IconButton icon="KeyRound" label="重置密码" onClick={() => setResetTarget(u)} />
                    {u.is_active ? (
                      <IconButton
                        icon="PowerOff"
                        label={
                          isSelf
                            ? '不能停用当前登录账号'
                            : u.is_last_admin
                              ? '最后一个管理员不能停用'
                              : '停用账号'
                        }
                        disabled={disableBlocked}
                        onClick={() => setDisableTarget(u)}
                      />
                    ) : (
                      <IconButton icon="Power" label="启用账号" onClick={() => enableUser(u)} />
                    )}
                    <IconButton
                      icon="Trash2"
                      label={
                        isSelf
                          ? '不能删除当前登录账号'
                          : u.is_last_admin
                            ? '最后一个管理员不能删除'
                            : '删除账号'
                      }
                      disabled={deleteBlocked}
                      onClick={() => {
                        setDeleteText('')
                        setDeleteTarget(u)
                      }}
                    />
                  </span>
                )
              },
            },
          ]}
          rows={data?.results ?? []}
          rowKey={(u) => u.id}
          loading={!data}
          empty={
            <EmptyState
              icon={deletedView ? 'Archive' : 'Users'}
              title={deletedView ? '没有已删除账号' : search ? '没有匹配的账号' : '还没有账号'}
              description={
                deletedView
                  ? '删除账号后会保留在这里，便于追溯历史会话与审计'
                  : search
                    ? '换个关键词试试'
                    : '创建第一个账号，并为其分配角色与部门'
              }
              action={
                !deletedView && !search ? (
                  <Button variant="primary" icon="UserPlus" onClick={openCreate}>
                    新建账号
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

      <UserForm
        open={formOpen}
        user={formUser}
        departments={departments}
        onClose={() => setFormOpen(false)}
        onSubmit={submitForm}
      />

      <ResetPasswordDialog
        target={resetTarget}
        onClose={() => setResetTarget(null)}
        onSubmit={submitReset}
      />

      <SessionDialog
        target={sessionTarget}
        isSelf={sessionTarget?.id === me?.id}
        onClose={() => setSessionTarget(null)}
      />

      <ConfirmDialog
        open={disableTarget !== null}
        title="停用账号"
        tone="danger"
        confirmText="确认停用"
        loading={update.isPending}
        onConfirm={confirmDisable}
        onCancel={() => setDisableTarget(null)}
      >
        停用后「{disableTarget?.username}」将无法登录，其全部 refresh token 立即吊销；已签发的 access token
        最长 60 分钟内仍可访问，到期即失效。可在本页随时重新启用。
      </ConfirmDialog>

      <ConfirmDialog
        open={downgrade !== null}
        title="降为普通用户"
        tone="danger"
        confirmText="确认降级"
        loading={update.isPending}
        onConfirm={confirmDowngrade}
        onCancel={() => setDowngrade(null)}
      >
        降级后「{downgrade?.user.username}」将立即失去用户管理、部门管理与系统设置的入口，已登录会话在下一次身份校准后按普通用户处理。
        系统必须保留至少一个有效管理员，最后一个管理员会被后端拒绝。
      </ConfirmDialog>

      <Modal
        open={deleteTarget !== null}
        title={`删除账号 · ${deleteTarget?.username ?? ''}`}
        icon="TriangleAlert"
        onClose={del.isPending ? () => undefined : () => setDeleteTarget(null)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleteTarget(null)} disabled={del.isPending}>
              取消
            </Button>
            <Button
              variant="danger"
              icon="Trash2"
              loading={del.isPending}
              disabled={!deleteMatches}
              onClick={confirmDelete}
            >
              确认删除
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-[10px]">
          <div className="rounded-md border border-state-error-border bg-state-error-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-error">
            <div>删除后的影响：</div>
            <ul className="mt-[4px] list-disc pl-[16px]">
              <li>账号立即停用并标记为已删除，无法登录，用户名不能复用；</li>
              <li>历史会话与消息、对话生成文件、审计日志全部保留用于追溯，不级联删除；</li>
              <li>该账号从常规列表移入「已删除账号」，可在那里继续查到。</li>
            </ul>
          </div>
          <Field
            label={`请输入用户名「${deleteTarget?.username ?? ''}」以确认`}
            hint="只有完整输入用户名后，「确认删除」按钮才可用"
          >
            <Input value={deleteText} onChange={(e) => setDeleteText(e.target.value)} autoComplete="off" />
          </Field>
        </div>
      </Modal>
    </PageContainer>
  )
}
