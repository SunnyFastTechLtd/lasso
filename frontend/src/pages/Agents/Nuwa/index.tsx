import { useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { Field, Input, Select, Textarea } from '@/components/ui/Field'
import { Modal } from '@/components/ui/Overlay'
import { SectionLabel } from '@/components/ui/Panel'
import { Switch } from '@/components/ui/Switch'
import { ApiError } from '@/api/request'
import {
  MARKET_DEGRADED_TEXT,
  nuwaApi,
  type KnowledgeBaseOption,
  type KnowledgeItem,
  type MarketCandidate,
  type MatchResult,
  type NuwaSession,
  type NuwaSkeleton,
} from '@/api/nuwa'
import { toast } from '@/store/toast'

interface Props {
  open: boolean
  onClose: () => void
  onCreated: (agentId: number) => void
}

const STEP_LABELS = ['描述需求', '确认骨架', '选工具与技能', '选资料', '确认生成']

const EXAMPLES = [
  '给销售做报价单助手：按产品目录生成报价单，输出 Excel 附件，面向一线销售',
  '帮人事回答入转调离流程问题，依据公司制度，回答要给出处',
  '合同风险初筛：标出付款、违约、知识产权条款风险，给法务复核',
]

const EMPTY_SKELETON: NuwaSkeleton = {
  name: '',
  description: '',
  job_tags: [],
  icon: 'Bot',
  prompt: '',
  speech_tier: 'mid',
  retrieval_tier: 'standard',
  welcome_message: '',
  visibility: 'private',
  knowledge_queries: [],
  model_name: '',
}

export function NuwaWizard({ open, onClose, onCreated }: Props) {
  const [session, setSession] = useState<NuwaSession | null>(null)
  const [step, setStep] = useState(1)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  const [brief, setBrief] = useState('')
  const [audience, setAudience] = useState('')
  const [department, setDepartment] = useState('')
  const [skeleton, setSkeleton] = useState<NuwaSkeleton>(EMPTY_SKELETON)
  const [models, setModels] = useState<string[]>([])
  const [matchQuery, setMatchQuery] = useState('')
  const [pickedMcp, setPickedMcp] = useState<number[]>([])
  const [pickedSkills, setPickedSkills] = useState<number[]>([])
  const [urls, setUrls] = useState('')
  const [discover, setDiscover] = useState(false)
  const [knowledgeItems, setKnowledgeItems] = useState<KnowledgeItem[]>([])
  const [pickedUrls, setPickedUrls] = useState<string[]>([])
  const [kbList, setKbList] = useState<KnowledgeBaseOption[]>([])
  const [kbId, setKbId] = useState<string>('')
  const [kbName, setKbName] = useState('')
  const [created, setCreated] = useState<{
    agent_id: number
    mounted: { mcp: { name: string }[]; skills: { name: string }[] }
    pending_imports: { title: string; kind: string; risk_flags: string[] }[]
    warnings: string[]
  } | null>(null)

  useEffect(() => {
    if (!open) return
    setStep(1)
    setSession(null)
    setBrief('')
    setAudience('')
    setDepartment('')
    setSkeleton(EMPTY_SKELETON)
    setError('')
    setPickedMcp([])
    setPickedSkills([])
    setUrls('')
    setKnowledgeItems([])
    setPickedUrls([])
    setCreated(null)
    setKbName('')
    setKbId('')
    nuwaApi
      .agentOptions()
      .then((data) => setModels(data.models ?? []))
      .catch(() => setModels([]))
    nuwaApi
      .knowledgeBases()
      .then((data) => setKbList(Array.isArray(data) ? data : (data.results ?? [])))
      .catch(() => setKbList([]))
  }, [open])

  function applySession(next: NuwaSession) {
    setSession(next)
    const merged = { ...EMPTY_SKELETON, ...(next.generated ?? {}) } as NuwaSkeleton
    setSkeleton(merged)
    if (next.knowledge_items?.length) setKnowledgeItems(next.knowledge_items)
    setStep(Math.min(Math.max(next.step || 1, 1), 5))
    if (next.error) setError(next.error)
  }

  function fail(err: unknown) {
    if (err instanceof ApiError) {
      setError(err.message)
      toast.error(err.message)
    } else {
      setError((err as Error).message)
      toast.error((err as Error).message)
    }
  }

  async function generate() {
    if (brief.trim().length < 10) {
      toast.error('请至少用 10 个字描述需求')
      return
    }
    setBusy('generate')
    setError('')
    try {
      const next = await nuwaApi.createSession({
        brief: brief.trim(),
        audience: audience.trim(),
        department: department.trim(),
      })
      applySession(next)
    } catch (err) {
      fail(err)
    } finally {
      setBusy('')
    }
  }

  async function saveSkeleton(advance = false) {
    if (!session) return
    setBusy('skeleton')
    try {
      const next = await nuwaApi.updateSession(session.id, { generated: skeleton })
      applySession(next)
      if (advance) {
        await searchMarket()
      }
    } catch (err) {
      fail(err)
    } finally {
      setBusy('')
    }
  }

  async function regenerate() {
    if (!session) return
    setBusy('regenerate')
    try {
      applySession(await nuwaApi.regenerate(session.id))
      toast.success('已重新生成，原内容可在「返回上一步」后对照')
    } catch (err) {
      fail(err)
    } finally {
      setBusy('')
    }
  }

  async function searchMarket() {
    if (!session) return
    setBusy('match')
    setError('')
    try {
      const result = await nuwaApi.matches(session.id, {
        query: matchQuery.trim() || undefined,
        kinds: ['mcp', 'skill'],
      })
      setSession({ ...session, candidates: result, step: 3, status: 'matching' })
      setStep(3)
      const selection = session.selections ?? {}
      setPickedMcp(selection.mcp ?? [])
      setPickedSkills(selection.skills ?? [])
    } catch (err) {
      fail(err)
    } finally {
      setBusy('')
    }
  }

  async function savePicks(skip = false) {
    if (!session) return
    setBusy('picks')
    try {
      const mcp = skip ? [] : pickedMcp
      const skills = skip ? [] : pickedSkills
      await nuwaApi.saveSelections(session.id, { mcp, skills })
      const next = await nuwaApi.getSession(session.id)
      applySession(next)
      setStep(4)
      setSession({ ...next, step: 4 })
    } catch (err) {
      fail(err)
    } finally {
      setBusy('')
    }
  }

  async function previewKnowledge() {
    if (!session) return
    const list = urls
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
    if (!list.length) {
      toast.error('请先填写要抓取的网址（每行一条）')
      return
    }
    setBusy('preview')
    try {
      await nuwaApi.planKnowledge(session.id, { urls: list, discover, queries: skeleton.knowledge_queries })
      const result = await nuwaApi.previewKnowledge(session.id, { urls: list, discover })
      setKnowledgeItems(result.items ?? [])
      setPickedUrls((result.items ?? []).filter((item) => item.status === 'ok').map((i) => i.url))
      if (result.message) toast.info(result.message)
      else toast.success(`抓取完成：成功 ${result.ok ?? 0}，失败 ${result.failed ?? 0}`)
    } catch (err) {
      fail(err)
    } finally {
      setBusy('')
    }
  }

  async function commitKnowledge(skip = false) {
    if (!session) return
    setBusy('commit')
    try {
      if (!skip && pickedUrls.length) {
        await nuwaApi.commitKnowledge(session.id, {
          urls: pickedUrls,
          ...(kbId ? { kb_id: Number(kbId) } : { kb_name: kbName.trim() || undefined }),
        })
        toast.success(`已入库 ${pickedUrls.length} 条资料`)
      }
      const next = await nuwaApi.getSession(session.id)
      applySession(next)
      setSession({ ...next, step: 5 })
      setStep(5)
    } catch (err) {
      fail(err)
    } finally {
      setBusy('')
    }
  }

  async function createAgent() {
    if (!session) return
    setBusy('create')
    try {
      const result = await nuwaApi.createAgent(session.id)
      setCreated(result)
      toast.success('已生成智能体草稿')
    } catch (err) {
      fail(err)
    } finally {
      setBusy('')
    }
  }

  const matchResult = (session?.candidates ?? null) as MatchResult | null
  const mcpCandidates = useMemo(
    () => (matchResult?.items ?? []).filter((item) => item.kind === 'mcp'),
    [matchResult],
  )
  const skillCandidates = useMemo(
    () => (matchResult?.items ?? []).filter((item) => item.kind === 'skill'),
    [matchResult],
  )
  const pickedCount = pickedMcp.length + pickedSkills.length

  function toggle(list: number[], setList: (next: number[]) => void, id: number) {
    setList(list.includes(id) ? list.filter((item) => item !== id) : [...list, id])
  }

  return (
    <Modal
      open={open}
      title="女娲模式：用一句话造智能体"
      icon="Sparkles"
      wide
      onClose={onClose}
      footer={
        <div className="flex w-full items-center justify-between">
          <div className="text-xs text-muted-foreground">
            第 {step} / 5 步 · 全过程只产出草稿，发布仍需走发布检查
          </div>
          <div className="flex items-center gap-[8px]">
            {step > 1 && !created && (
              <Button onClick={() => setStep(step - 1)} disabled={!!busy}>
                上一步
              </Button>
            )}
            <Button onClick={onClose}>{created ? '关闭' : '取消'}</Button>
          </div>
        </div>
      }
    >
      <div className="space-y-[14px]">
        <div className="flex items-center gap-[6px]">
          {STEP_LABELS.map((label, index) => (
            <div
              key={label}
              className={`flex-1 rounded px-[8px] py-[6px] text-center text-xs ${
                index + 1 === step
                  ? 'bg-primary/15 font-medium text-primary'
                  : index + 1 < step
                    ? 'bg-surface-2 text-muted-foreground'
                    : 'bg-surface-1 text-muted-foreground'
              }`}
            >
              {index + 1}. {label}
            </div>
          ))}
        </div>

        {error && (
          <div className="rounded border border-state-error/40 bg-state-error/10 px-[10px] py-[8px] text-xs leading-[1.6] text-state-error">
            {error}
          </div>
        )}

        {step === 1 && (
          <div className="space-y-[12px]">
            <Field
              label="描述你想要的智能体"
              required
              hint="写清做什么、给谁用、输出什么；至少 10 个字"
            >
              <Textarea
                rows={5}
                value={brief}
                placeholder="例：给销售做报价单助手，按产品目录生成报价单并导出 Excel……"
                onChange={(e) => setBrief(e.target.value)}
              />
            </Field>
            <div className="flex flex-wrap gap-[6px]">
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  type="button"
                  className="rounded border border-border/60 px-[8px] py-[4px] text-xs text-muted-foreground hover:border-primary/50 hover:text-primary"
                  onClick={() => setBrief(example)}
                >
                  {example.slice(0, 18)}…
                </button>
              ))}
            </div>
            <div className="grid grid-cols-2 gap-[10px]">
              <Field label="使用对象">
                <Input
                  value={audience}
                  placeholder="如：一线销售 / 人事专员"
                  onChange={(e) => setAudience(e.target.value)}
                />
              </Field>
              <Field label="面向部门" hint="用于建议可见范围，可留空">
                <Input
                  value={department}
                  placeholder="如：销售部"
                  onChange={(e) => setDepartment(e.target.value)}
                />
              </Field>
            </div>
            <Button variant="primary" icon="Sparkles" loading={busy === 'generate'} onClick={generate}>
              生成智能体骨架
            </Button>
          </div>
        )}

        {step === 2 && session && (
          <div className="space-y-[12px]">
            {session.error && (
              <div className="rounded border border-state-warning/40 bg-state-warning/10 px-[10px] py-[8px] text-xs leading-[1.6] text-state-warning">
                模型生成未完成：{session.error}（下面是可直接编辑的空白骨架）
              </div>
            )}
            <div className="grid grid-cols-2 gap-[10px]">
              <Field label="名称" required hint="2–30 字，重名会自动加后缀">
                <Input
                  value={skeleton.name}
                  maxLength={30}
                  onChange={(e) => setSkeleton({ ...skeleton, name: e.target.value })}
                />
              </Field>
              <Field label="模型">
                <Select
                  value={skeleton.model_name}
                  onChange={(e) => setSkeleton({ ...skeleton, model_name: e.target.value })}
                >
                  <option value="">平台默认</option>
                  {models.map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <Field label="定位描述" hint="≤200 字，会展示在使用端卡片上">
              <Textarea
                rows={2}
                value={skeleton.description}
                onChange={(e) => setSkeleton({ ...skeleton, description: e.target.value })}
              />
            </Field>
            <Field
              label="系统提示词"
              hint="可用占位符只有 {current_date} {user_name} {department}"
            >
              <Textarea
                rows={7}
                value={skeleton.prompt}
                onChange={(e) => setSkeleton({ ...skeleton, prompt: e.target.value })}
              />
            </Field>
            {skeleton.unknown_placeholders?.length ? (
              <div className="text-xs text-state-warning">
                平台不会替换这些占位符：{skeleton.unknown_placeholders.join('、')}
              </div>
            ) : null}
            <div className="grid grid-cols-3 gap-[10px]">
              <Field label="说话方式">
                <Select
                  value={skeleton.speech_tier}
                  onChange={(e) =>
                    setSkeleton({ ...skeleton, speech_tier: e.target.value as NuwaSkeleton['speech_tier'] })
                  }
                >
                  <option value="low">照本宣科</option>
                  <option value="mid">平衡</option>
                  <option value="high">多发挥</option>
                </Select>
              </Field>
              <Field label="查询次数">
                <Select
                  value={skeleton.retrieval_tier}
                  onChange={(e) =>
                    setSkeleton({
                      ...skeleton,
                      retrieval_tier: e.target.value as NuwaSkeleton['retrieval_tier'],
                    })
                  }
                >
                  <option value="few">一次查不到就直说</option>
                  <option value="standard">允许少量补查</option>
                  <option value="many">允许多次查询</option>
                </Select>
              </Field>
              <Field label="可见范围建议">
                <Select
                  value={skeleton.visibility}
                  onChange={(e) =>
                    setSkeleton({ ...skeleton, visibility: e.target.value as NuwaSkeleton['visibility'] })
                  }
                >
                  <option value="private">仅自己</option>
                  <option value="department">指定部门</option>
                  <option value="public">全企业</option>
                </Select>
              </Field>
            </div>
            <Field label="欢迎语" hint="对话页开场白，≤120 字">
              <Input
                value={skeleton.welcome_message}
                maxLength={120}
                onChange={(e) => setSkeleton({ ...skeleton, welcome_message: e.target.value })}
              />
            </Field>
            <div className="flex items-center gap-[8px]">
              <Button
                variant="primary"
                icon="ArrowRight"
                loading={busy === 'skeleton' || busy === 'match'}
                onClick={() => saveSkeleton(true)}
              >
                保存并查找工具
              </Button>
              <Button icon="RefreshCw" loading={busy === 'regenerate'} onClick={regenerate}>
                重新生成
              </Button>
            </div>
          </div>
        )}

        {step === 3 && (
          <div className="space-y-[12px]">
            <div className="flex items-center gap-[8px]">
              <Input
                value={matchQuery}
                placeholder="可换个检索词，如：excel / postgres / pdf"
                onChange={(e) => setMatchQuery(e.target.value)}
              />
              <Button icon="Search" loading={busy === 'match'} onClick={searchMarket}>
                重新检索
              </Button>
            </div>
            {matchResult && (
              <div className="rounded border border-border/60 bg-surface-1 px-[10px] py-[8px] text-xs leading-[1.6]">
                检索方式：
                <span className="font-medium">
                  {matchResult.mode === 'online'
                    ? '实时（公网）'
                    : matchResult.mode === 'mixed'
                      ? '实时 + 本地缓存'
                      : '本地缓存'}
                </span>
                {matchResult.cache_synced_at && (
                  <span className="text-muted-foreground">
                    {' '}
                    · 缓存同步于 {matchResult.cache_synced_at.slice(0, 10)}
                  </span>
                )}
                {matchResult.degraded_reason && (
                  <details className="mt-[4px] text-state-warning">
                    <summary className="cursor-pointer select-none">
                      检索已降级{matchResult.mode === 'cached' ? '，当前展示本地缓存' : ''}
                    </summary>
                    <div className="mt-[2px] leading-[1.6]">
                      {MARKET_DEGRADED_TEXT[matchResult.degraded_reason] ?? matchResult.degraded_reason}
                    </div>
                  </details>
                )}
              </div>
            )}
            <CandidateGroup
              title="MCP 工具来源"
              empty="没有匹配的工具来源"
              items={mcpCandidates}
              picked={pickedMcp}
              onToggle={(id) => toggle(pickedMcp, setPickedMcp, id)}
            />
            <CandidateGroup
              title="技能规范"
              empty="没有匹配的技能"
              items={skillCandidates}
              picked={pickedSkills}
              onToggle={(id) => toggle(pickedSkills, setPickedSkills, id)}
            />
            <div className="flex items-center gap-[8px]">
              <Button
                variant="primary"
                icon="Check"
                loading={busy === 'picks'}
                onClick={() => savePicks(false)}
              >
                保存勾选（{pickedCount}）
              </Button>
              <Button onClick={() => savePicks(true)} disabled={!!busy}>
                跳过
              </Button>
              <span className="text-xs text-muted-foreground">
                外部条目需要管理员审批通过后才会真正接入；草稿不会等审批
              </span>
            </div>
          </div>
        )}

        {step === 4 && session && (
          <div className="space-y-[12px]">
            <Field
              label="抓取网址"
              hint="每行一条；同域浅层只抓同站点页面，最多 20 条"
            >
              <Textarea
                rows={3}
                value={urls}
                placeholder="https://example.com/pricing"
                onChange={(e) => setUrls(e.target.value)}
              />
            </Field>
            <div className="flex items-center justify-between rounded border border-border/60 px-[10px] py-[8px]">
              <div>
                <div className="text-xs font-medium">同域浅层发现</div>
                <div className="text-xs text-muted-foreground">
                  先读 sitemap.xml，取不到再抓首页上的同域链接
                </div>
              </div>
              <Switch checked={discover} onChange={setDiscover} />
            </div>
            <div className="flex items-center gap-[8px]">
              <Button icon="Download" loading={busy === 'preview'} onClick={previewKnowledge}>
                抓取预览
              </Button>
              <span className="text-xs text-muted-foreground">
                只抓公网 https、遵守 robots.txt、同域 1 秒 1 次
              </span>
            </div>
            {knowledgeItems.length > 0 && (
              <div className="max-h-[220px] space-y-[6px] overflow-auto rounded border border-border/60 p-[8px]">
                {knowledgeItems.map((item) => (
                  <label
                    key={item.url}
                    className="flex items-start gap-[8px] rounded px-[6px] py-[4px] hover:bg-surface-2"
                  >
                    <input
                      type="checkbox"
                      className="mt-[3px]"
                      disabled={item.status !== 'ok'}
                      checked={pickedUrls.includes(item.url)}
                      onChange={() =>
                        setPickedUrls((prev) =>
                          prev.includes(item.url)
                            ? prev.filter((url) => url !== item.url)
                            : [...prev, item.url],
                        )
                      }
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs">{item.title || item.url}</span>
                      <span className="block truncate text-xs text-muted-foreground">
                        {item.site} · {item.status === 'ok' ? `${item.words} 字` : item.reason}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            )}
            <div className="grid grid-cols-2 gap-[10px]">
              <Field label="写入已有知识库">
                <Select value={kbId} onChange={(e) => setKbId(e.target.value)}>
                  <option value="">（新建）</option>
                  {kbList.map((kb) => (
                    <option key={kb.id} value={String(kb.id)}>
                      {kb.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="新知识库名称" hint="留空则用「女娲资料库」">
                <Input
                  value={kbName}
                  disabled={!!kbId}
                  onChange={(e) => setKbName(e.target.value)}
                />
              </Field>
            </div>
            <div className="flex items-center gap-[8px]">
              <Button
                variant="primary"
                icon="Check"
                loading={busy === 'commit'}
                onClick={() => commitKnowledge(false)}
              >
                入库并继续（{pickedUrls.length}）
              </Button>
              <Button onClick={() => commitKnowledge(true)} disabled={!!busy}>
                跳过资料
              </Button>
            </div>
          </div>
        )}

        {step === 5 && session && (
          <div className="space-y-[12px]">
            <SectionLabel>将创建的草稿</SectionLabel>
            <div className="rounded border border-border/60 p-[10px] text-xs leading-[1.8]">
              <div>
                名称：<span className="font-medium">{skeleton.name || '（未命名）'}</span>
              </div>
              <div>模型：{skeleton.model_name || '平台默认'}</div>
              <div>可见范围：{skeleton.visibility === 'public' ? '全企业' : skeleton.visibility === 'department' ? '指定部门' : '仅自己'}</div>
              <div>
                挂载：工具 {pickedMcp.length} 个 · 技能 {pickedSkills.length} 个 · 知识库{' '}
                {session.knowledge_plan?.kb_id ? 1 : 0} 个
              </div>
            </div>
            {created ? (
              <div className="space-y-[8px]">
                <div className="text-xs text-state-success">草稿已创建（#{created.agent_id}）</div>
                {created.warnings.map((warning) => (
                  <div key={warning} className="text-xs text-state-warning">
                    {warning}
                  </div>
                ))}
                <Button
                  variant="primary"
                  icon="ArrowRight"
                  onClick={() => {
                    onCreated(created.agent_id)
                    onClose()
                  }}
                >
                  打开装配页继续完善
                </Button>
              </div>
            ) : (
              <div className="space-y-[8px]">
                <div className="text-xs text-muted-foreground">
                  创建后仍是草稿：模型、挂载、权限都可以在装配页继续改，发布需通过发布检查。
                </div>
                <Button variant="primary" icon="Plus" loading={busy === 'create'} onClick={createAgent}>
                  创建草稿
                </Button>
              </div>
            )}
          </div>
        )}
      </div>
    </Modal>
  )
}

function CandidateGroup({
  title,
  empty,
  items,
  picked,
  onToggle,
}: {
  title: string
  empty: string
  items: MarketCandidate[]
  picked: number[]
  onToggle: (id: number) => void
}) {
  return (
    <div className="space-y-[6px]">
      <SectionLabel>{title}</SectionLabel>
      {!items.length && <div className="text-xs text-muted-foreground">{empty}</div>}
      <div className="max-h-[200px] space-y-[6px] overflow-auto">
        {items.map((item) => (
          <label
            key={item.id}
            className="flex items-start gap-[8px] rounded border border-border/60 px-[8px] py-[6px] hover:border-primary/40"
          >
            <input
              type="checkbox"
              className="mt-[3px]"
              checked={picked.includes(item.id)}
              onChange={() => onToggle(item.id)}
            />
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-[6px]">
                <span className="truncate text-xs font-medium">{item.title}</span>
                <span className="shrink-0 rounded bg-surface-2 px-[4px] py-[1px] text-xs text-muted-foreground">
                  {item.source}
                </span>
                {item.requires_egress && (
                  <span className="shrink-0 rounded bg-state-warning/15 px-[4px] py-[1px] text-xs text-state-warning">
                    需出网
                  </span>
                )}
                {!item.license_declared && (
                  <span className="shrink-0 rounded bg-state-warning/15 px-[4px] py-[1px] text-xs text-state-warning">
                    许可证未声明
                  </span>
                )}
              </span>
              <span className="mt-[2px] block truncate text-xs text-muted-foreground">
                {item.summary || '（无摘要）'}
              </span>
              <span className="mt-[2px] block text-xs text-muted-foreground">
                匹配 {Math.round(item.score * 100)}% · {item.reason}
              </span>
            </span>
          </label>
        ))}
      </div>
    </div>
  )
}
