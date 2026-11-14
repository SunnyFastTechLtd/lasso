import type { CSSProperties } from 'react'
import { Circle } from 'lucide-react'
import { ICON_MAP } from './icons'

interface IconProps {
  name: string
  size?: number
  className?: string
  style?: CSSProperties
}

/** 统一图标入口：设计稿度量 15px / stroke 1.5 */
export function Icon({ name, size = 15, className, style }: IconProps) {
  const Cmp = ICON_MAP[name]
  if (!Cmp) {
    // 未注册图标：渲染可见的占位圆点并标注原名，避免静默空白
    if (import.meta.env.DEV) console.warn(`[Icon] 未注册的图标名「${name}」，已回退为 Circle`)
    return (
      <Circle
        size={size}
        strokeWidth={1.5}
        className={className}
        style={style}
        data-icon-missing={name}
        aria-label={`未注册图标：${name}`}
      />
    )
  }
  return <Cmp size={size} strokeWidth={1.5} className={className} style={style} />
}
