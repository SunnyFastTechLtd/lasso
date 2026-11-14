import { Panel } from '@/components/ui/Panel'
import { Seg } from '@/components/ui/Seg'
import { SKINS, useThemeStore, type SkinOption, type ThemeMode } from '@/store/theme'

const OPTIONS: { value: ThemeMode; label: string }[] = [
  { value: 'dark', label: '深色' },
  { value: 'light', label: '浅色' },
  { value: 'system', label: '跟随系统' },
]

function SkinSwatch({ skin }: { skin: SkinOption }) {
  return (
    <span
      className="flex h-[20px] w-[30px] flex-none overflow-hidden rounded-sm border border-border"
      aria-hidden="true"
    >
      <span className="h-full w-[10px]" style={{ background: skin.preview.primary }} />
      <span className="h-full flex-1" style={{ background: skin.preview.background }} />
      <span className="h-full w-[10px]" style={{ background: skin.preview.card }} />
    </span>
  )
}

export function AppearancePanel() {
  const mode = useThemeStore((s) => s.mode)
  const resolved = useThemeStore((s) => s.resolved)
  const skin = useThemeStore((s) => s.skin)
  const setMode = useThemeStore((s) => s.setMode)
  const setSkin = useThemeStore((s) => s.setSkin)

  return (
    <Panel title="外观" icon="Palette" meta="仅影响当前浏览器" padded>
      <div className="flex flex-wrap items-center gap-[12px]">
        <span className="text-sm text-foreground">主题模式</span>
        <Seg items={OPTIONS} value={mode} onChange={setMode} />
        <span className="text-xs text-muted-foreground">
          当前生效：{resolved === 'dark' ? '深色' : '浅色'}
        </span>
      </div>
      <p className="field-hint">
        深色为默认；「跟随系统」会随操作系统外观实时切换。选择保存在本机浏览器，刷新后保持。
      </p>

      <div className="mt-[14px] flex flex-wrap items-center gap-[12px]">
        <span className="text-sm text-foreground">皮肤</span>
        <span className="text-xs text-muted-foreground">
          当前：{SKINS.find((s) => s.value === skin)?.label}
        </span>
      </div>
      <div className="mt-[8px] flex flex-wrap gap-[8px]" role="radiogroup" aria-label="皮肤">
        {SKINS.map((item) => {
          const active = item.value === skin
          return (
            <button
              key={item.value}
              type="button"
              role="radio"
              aria-checked={active}
              title={item.note}
              onClick={() => setSkin(item.value)}
              className={`flex h-[40px] items-center gap-[8px] rounded-md border px-[10px] text-sm transition-colors ${
                active
                  ? 'border-primary bg-state-info-soft text-foreground'
                  : 'border-border bg-muted text-muted-foreground hover:text-foreground'
              }`}
            >
              <SkinSwatch skin={item} />
              <span>{item.label}</span>
            </button>
          )
        })}
      </div>
      <p className="field-hint">皮肤与明暗相互独立，可组合使用；同样只作用于当前浏览器。</p>
    </Panel>
  )
}
