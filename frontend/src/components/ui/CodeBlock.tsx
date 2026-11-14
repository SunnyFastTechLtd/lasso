interface CodeBlockProps {
  code: string
  maxHeight?: number
  className?: string
}

/** 配置/命令预览块（等宽、不折行、横向滚动） */
export function CodeBlock({ code, maxHeight, className = '' }: CodeBlockProps) {
  return (
    <pre className={`code-block ${className}`} style={maxHeight ? { maxHeight, overflowY: 'auto' } : undefined}>
      {code}
    </pre>
  )
}
