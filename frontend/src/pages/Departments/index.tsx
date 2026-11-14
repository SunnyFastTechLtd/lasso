import { useEffect, useState } from 'react'
import { PageContainer, PageHeader } from '@/components/AppShell'
import { Button, IconButton } from '@/components/ui/Button'
import { DataTable } from '@/components/ui/DataTable'
import { EmptyState } from '@/components/ui/EmptyState'
import { Field, Input, Textarea } from '@/components/ui/Field'
import { Icon } from '@/components/ui/Icon'
import { Modal } from '@/components/ui/Overlay'
import { Panel } from '@/components/ui/Panel'
import {
  useCreateDepartment,
  useDepartments,
  useUpdateDepartment,
  type Department,
} from '@/api/departments'
import { toast } from '@/store/toast'
import { readableError } from '@/utils/password'
import { fmtRelative } from '@/utils/format'

interface FormState {
  id: number | null
  name: string
  description: string
}

const EMPTY_FORM: FormState = { id: null, name: '', description: '' }

export default function DepartmentsPage() {
  const { data: departments = [], isFetching } = useDepartments(true)
  const create = useCreateDepartment()
  const update = useUpdateDepartment()

  const [form, setForm] = useState<FormState | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (form) setError('')
  }, [form])

  const isEdit = form?.id != null
  const saving = create.isPending || update.isPending

  function openCreate() {
    setForm({ ...EMPTY_FORM })
  }

  function openEdit(dept: Department) {
    setForm({ id: dept.id, name: dept.name, description: dept.description })
  }

  async function submit() {
    if (!form) return
    const name = form.name.trim()
    if (!name) {
      setError('请填写部门名称')
      return
    }
    const duplicate = departments.some(
      (d) => d.name === name && d.id !== form.id,
    )
    if (duplicate) {
      setError('部门名称已存在')
      return
    }
    setError('')
    try {
      if (form.id == null) {
        await create.mutateAsync({ name, description: form.description.trim() })
        toast.success(`已创建部门「${name}」`)
      } else {
        const renamed = departments.find((d) => d.id === form.id)?.name !== name
        await update.mutateAsync({
          id: form.id,
          payload: { name, description: form.description.trim() },
        })
        toast.success(renamed ? `已重命名部门为「${name}」，该部门用户的归属已同步` : '已更新部门描述')
      }
      setForm(null)
    } catch (err) {
      setError(readableError(err))
    }
  }

  return (
    <PageContainer>
      <PageHeader
        title="部门管理"
        subtitle="部门用于用户归属与可见范围；重命名会同步该部门下所有用户的归属"
        actions={
          <Button variant="primary" icon="Plus" onClick={openCreate}>
            新建部门
          </Button>
        }
      />

      <div className="mt-[14px] flex items-start gap-[6px] rounded-md border border-border bg-surface-2 px-[10px] py-[8px] text-xs leading-relaxed text-muted-foreground">
        <Icon name="Info" size={13} className="mt-[1px] flex-none text-primary" />
        <span>
          部门层级（parent）暂不在界面开放：层级会级联影响技能与工具来源的可见范围，误改会静默扩大或收窄授权，需先补成环校验与授权复核后再开放。
          本页按平铺部门维护。
        </span>
      </div>

      <div className="page-toolbar">
        <span className="toolbar-note">
          {isFetching ? '读取中…' : `共 ${departments.length} 个部门`}
        </span>
      </div>

      <Panel padded={false}>
        <DataTable
          columns={[
            {
              key: 'name',
              title: '部门名称',
              render: (d) => (
                <span className="row-main">
                  <span className="row-title">{d.name}</span>
                  <span className="row-sub truncate">{d.description || '暂无描述'}</span>
                </span>
              ),
            },
            {
              key: 'created',
              title: '创建时间',
              width: 140,
              render: (d) => <span className="text-xs text-muted-foreground">{fmtRelative(d.created_at)}</span>,
            },
            {
              key: 'ops',
              title: '操作',
              width: 72,
              render: (d) => (
                <span className="flex items-center justify-end">
                  <IconButton icon="Pencil" label="重命名 / 编辑描述" onClick={() => openEdit(d)} />
                </span>
              ),
            },
          ]}
          rows={departments}
          rowKey={(d) => d.id}
          loading={isFetching && departments.length === 0}
          empty={
            <EmptyState
              icon="Building2"
              title="还没有部门"
              description="先建一个部门，再在用户管理中把成员分配进去"
              action={
                <Button variant="primary" icon="Plus" onClick={openCreate}>
                  新建部门
                </Button>
              }
            />
          }
        />
      </Panel>

      <Modal
        open={form !== null}
        title={isEdit ? `编辑部门 · ${form?.name ?? ''}` : '新建部门'}
        icon="Building2"
        onClose={saving ? () => undefined : () => setForm(null)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setForm(null)} disabled={saving}>
              取消
            </Button>
            <Button variant="primary" icon="Check" loading={saving} onClick={submit}>
              {isEdit ? '保存修改' : '创建部门'}
            </Button>
          </>
        }
      >
        {form && (
          <div className="flex flex-col gap-[10px]">
            {error && (
              <div className="rounded-md border border-state-error-border bg-state-error-soft px-[10px] py-[8px] text-xs leading-relaxed text-state-error">
                {error}
              </div>
            )}
            <Field
              label="部门名称"
              required
              hint={isEdit ? '重命名会同步更新该部门下所有用户的部门字段；历史知识库的部门字符串不变' : '在同一企业内唯一'}
            >
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="如：产品研发部"
                maxLength={64}
                autoFocus
              />
            </Field>
            <Field label="描述">
              <Textarea
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="这个部门负责什么（选填）"
                rows={3}
                maxLength={200}
              />
            </Field>
          </div>
        )}
      </Modal>
    </PageContainer>
  )
}
