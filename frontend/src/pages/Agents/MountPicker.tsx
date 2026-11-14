import { useState } from 'react'
import { Icon } from '@/components/ui/Icon'
import { SectionLabel } from '@/components/ui/Panel'
import type { MountOptions } from '@/types/agent'

/** 四分区挂载选择结果（A4）：与写入面 mcp_mounts/skill_ids/... 一一对应 */
export interface MountSelection {
  mcp: { server: number; tool_names: string[] }[]
  skill_ids: number[]
  knowledge_base_ids: number[]
  builtin_capabilities: string[]
}

export function selectionFromMountOptions(o: MountOptions): MountSelection {
  return {
    mcp: o.mcp_servers
      .filter((s) => s.checked)
      .map((s) => ({ server: s.id, tool_names: s.tools.filter((t) => t.checked).map((t) => t.name) })),
    skill_ids: o.skills.filter((s) => s.checked).map((s) => s.id),
    knowledge_base_ids: o.knowledge_bases.filter((k) => k.checked).map((k) => k.id),
    builtin_capabilities: o.builtin_capabilities.filter((c) => c.checked).map((c) => c.value),
  }
}

interface Props {
  options: MountOptions
  value: MountSelection
  onChange: (next: MountSelection) => void
  readOnly?: boolean
}

export function MountPicker({ options, value, onChange, readOnly = false }: Props) {
  const [expanded, setExpanded] = useState<Record<number, boolean>>({})

  function mountEntry(serverId: number) {
    return value.mcp.find((m) => m.server === serverId) ?? null
  }

  function toggleServer(serverId: number, allToolNames: string[]) {
    const cur = mountEntry(serverId)
    if (cur) {
      onChange({ ...value, mcp: value.mcp.filter((m) => m.server !== serverId) })
    } else {
      onChange({ ...value, mcp: [...value.mcp, { server: serverId, tool_names: [...allToolNames] }] })
    }
  }

  function toggleTool(serverId: number, toolName: string) {
    const cur = mountEntry(serverId)
    if (!cur) {
      onChange({ ...value, mcp: [...value.mcp, { server: serverId, tool_names: [toolName] }] })
    } else if (cur.tool_names.includes(toolName)) {
      const rest = cur.tool_names.filter((t) => t !== toolName)
      onChange({
        ...value,
        mcp: rest.length ? value.mcp.map((m) => (m.server === serverId ? { ...m, tool_names: rest } : m)) : value.mcp.filter((m) => m.server !== serverId),
      })
    } else {
      onChange({ ...value, mcp: value.mcp.map((m) => (m.server === serverId ? { ...m, tool_names: [...m.tool_names, toolName] } : m)) })
    }
  }

  function selectAllTools(serverId: number, toolNames: string[]) {
    const cur = mountEntry(serverId)
    onChange({
      ...value,
      mcp: cur
        ? value.mcp.map((m) => (m.server === serverId ? { ...m, tool_names: [...toolNames] } : m))
        : [...value.mcp, { server: serverId, tool_names: [...toolNames] }],
    })
  }

  return (
    <div className="space-y-[14px]">
      {options.conflicts.length > 0 && (
        <div className="rounded border border-state-warning/40 bg-state-warning/10 px-[10px] py-[8px]">
          <div className="flex items-center gap-[6px] text-xs font-medium text-state-warning">
            <Icon name="AlertTriangle" size={13} /> 不同来源存在同名工具（发布检查会再次核对）
          </div>
          <ul className="mt-[4px] space-y-[2px] text-xs text-muted-foreground">
            {options.conflicts.map((c) => (
              <li key={c.tool}>
                <span className="mono">{c.tool}</span> · {c.owners.join(' / ')}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* MCP 工具来源：可展开逐个勾工具 */}
      <div>
        <SectionLabel>MCP 工具来源</SectionLabel>
        {options.mcp_servers.length === 0 && (
          <p className="mt-[6px] text-xs text-muted-foreground">尚无已验证可用的工具来源，请先在「MCP 工具来源」页接入并测试通过。</p>
        )}
        <div className="mt-[6px] space-y-[6px]">
          {options.mcp_servers.map((s) => {
            const entry = mountEntry(s.id)
            const selectableTools = s.tools.filter((t) => t.selectable).map((t) => t.name)
            const openRow = expanded[s.id] ?? false
            return (
              <div key={s.id} className="rounded border border-border/60">
                <div className="flex items-center gap-[8px] px-[10px] py-[7px]">
                  <input
                    type="checkbox"
                    className="accent-primary"
                    checked={!!entry}
                    disabled={readOnly}
                    onChange={() => toggleServer(s.id, selectableTools)}
                  />
                  <button
                    type="button"
                    className="flex flex-1 items-center gap-[6px] text-left"
                    onClick={() => setExpanded((e) => ({ ...e, [s.id]: !openRow }))}
                  >
                    <Icon name={openRow ? 'ChevronDown' : 'ChevronRight'} size={13} className="text-muted-foreground" />
                    <span className="text-xs font-medium">{s.name}</span>
                    {s.is_builtin && (
                      <span className="rounded bg-muted px-[5px] py-[1px] text-xs text-muted-foreground">内置</span>
                    )}
                    {s.warning && (
                      <span className="text-xs text-state-warning" title={s.warning}>
                        {s.status_display === '已停用' ? '已停用，挂载视为未挂载' : '部分工具不可用'}
                      </span>
                    )}
                    {!s.warning && s.status !== 'active' && s.status !== 'builtin' && (
                      <span className="text-xs text-muted-foreground">{s.status_display}</span>
                    )}
                    <span className="ml-auto text-xs text-muted-foreground tnum">
                      {entry ? `${entry.tool_names.length}/${selectableTools.length} 工具` : `${s.tools.length} 工具`}
                      {s.used_by_agents > 0 && ` · ${s.used_by_agents} 个智能体在用`}
                    </span>
                  </button>
                </div>
                {openRow && (
                  <div className="border-t border-border/50 px-[10px] py-[6px]">
                    {s.tools.length === 0 && (
                      <p className="text-xs text-muted-foreground">尚未发现工具清单（该来源工具缓存为空）。</p>
                    )}
                    {entry && selectableTools.length > 0 && !readOnly && (
                      <div className="mb-[4px] flex justify-end">
                        <button
                          type="button"
                          className="text-xs text-primary hover:underline"
                          onClick={() =>
                            entry.tool_names.length > 0
                              ? onChange({ ...value, mcp: value.mcp.filter((m) => m.server !== s.id) })
                              : selectAllTools(s.id, selectableTools)
                          }
                        >
                          {entry.tool_names.length > 0 ? '取消挂载该来源' : '全选该来源'}
                        </button>
                      </div>
                    )}
                    <ul className="space-y-[3px]">
                      {s.tools.map((t) => (
                        <li key={t.name}>
                          <label
                            className={`flex items-center gap-[7px] rounded px-[6px] py-[3px] ${
                              t.selectable && !readOnly ? 'cursor-pointer hover:bg-muted/60' : 'opacity-60'
                            }`}
                            title={t.selectable ? t.description : t.reason}
                          >
                            <input
                              type="checkbox"
                              className="accent-primary"
                              disabled={!t.selectable || readOnly}
                              checked={!!entry?.tool_names.includes(t.name)}
                              onChange={() => toggleTool(s.id, t.name)}
                            />
                            <span className="mono text-xs">{t.full_name}</span>
                            <span className="truncate text-xs text-muted-foreground">
                              {t.display_name}
                              {t.description ? ` · ${t.description}` : ''}
                            </span>
                            {!t.selectable && (
                              <span className="ml-auto shrink-0 text-xs text-muted-foreground">{t.reason}</span>
                            )}
                          </label>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>

      {/* 技能规范：仅 active/builtin 可勾，其余灰置带原因 */}
      <div>
        <SectionLabel>技能规范（Skills）</SectionLabel>
        {options.skills.length === 0 && (
          <p className="mt-[6px] text-xs text-muted-foreground">暂无技能，请先在「技能规范」页上传并审核启用。</p>
        )}
        <ul className="mt-[6px] space-y-[3px]">
          {options.skills.map((sk) => {
            const checked = value.skill_ids.includes(sk.id)
            return (
              <li key={sk.id}>
                <label
                  className={`flex items-center gap-[7px] rounded px-[6px] py-[3px] ${
                    sk.selectable && !readOnly ? 'cursor-pointer hover:bg-muted/60' : 'opacity-60'
                  }`}
                  title={sk.selectable ? sk.description : sk.reason}
                >
                  <input
                    type="checkbox"
                    className="accent-primary"
                    disabled={!sk.selectable || readOnly}
                    checked={checked}
                    onChange={() =>
                      onChange({
                        ...value,
                        skill_ids: checked ? value.skill_ids.filter((x) => x !== sk.id) : [...value.skill_ids, sk.id],
                      })
                    }
                  />
                  <span className="text-xs">{sk.name}</span>
                  <span className="mono text-xs text-muted-foreground">{sk.slug}</span>
                  {sk.requires.length > 0 && (
                    <span className="text-xs text-muted-foreground" title={`依赖工具：${sk.requires.join(', ')}`}>
                      依赖 {sk.requires.length} 个工具
                    </span>
                  )}
                  {!sk.selectable && <span className="ml-auto text-xs text-muted-foreground">{sk.reason}</span>}
                </label>
              </li>
            )
          })}
        </ul>
      </div>

      {/* 知识库 */}
      <div>
        <SectionLabel>知识库</SectionLabel>
        {options.knowledge_bases.length === 0 ? (
          <p className="mt-[6px] text-xs text-muted-foreground">知识库尚未启用（P1 能力），当前版本默认不挂载。</p>
        ) : (
          <ul className="mt-[6px] space-y-[3px]">
            {options.knowledge_bases.map((k) => {
              const checked = value.knowledge_base_ids.includes(k.id)
              return (
                <li key={k.id}>
                  <label
                    className={`flex items-center gap-[7px] rounded px-[6px] py-[3px] ${readOnly ? 'opacity-60' : 'cursor-pointer hover:bg-muted/60'}`}
                  >
                    <input
                      type="checkbox"
                      className="accent-primary"
                      disabled={readOnly}
                      checked={checked}
                      onChange={() =>
                        onChange({
                          ...value,
                          knowledge_base_ids: checked
                            ? value.knowledge_base_ids.filter((x) => x !== k.id)
                            : [...value.knowledge_base_ids, k.id],
                        })
                      }
                    />
                    <span className="text-xs">{k.name}</span>
                    <span className="text-xs text-muted-foreground tnum">{k.document_count} 文档</span>
                    {k.empty && <span className="text-xs text-state-warning">空库</span>}
                  </label>
                </li>
              )
            })}
          </ul>
        )}
      </div>

      {/* 内置能力 */}
      <div>
        <SectionLabel>内置能力</SectionLabel>
        <div className="mt-[6px] flex flex-wrap gap-[8px]">
          {options.builtin_capabilities.map((c) => {
            const checked = value.builtin_capabilities.includes(c.value)
            return (
              <label
                key={c.value}
                className={`flex items-center gap-[6px] rounded border border-border/60 px-[8px] py-[4px] ${
                  readOnly ? 'opacity-60' : 'cursor-pointer hover:bg-muted/60'
                }`}
              >
                <input
                  type="checkbox"
                  className="accent-primary"
                  disabled={readOnly}
                  checked={checked}
                  onChange={() =>
                    onChange({
                      ...value,
                      builtin_capabilities: checked
                        ? value.builtin_capabilities.filter((x) => x !== c.value)
                        : [...value.builtin_capabilities, c.value],
                    })
                  }
                />
                <span className="text-xs">{c.label}</span>
                <span className="mono text-xs text-muted-foreground">{c.value}</span>
              </label>
            )
          })}
        </div>
      </div>
    </div>
  )
}
