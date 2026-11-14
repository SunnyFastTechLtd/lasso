// 集中管理 query key，避免各页面散落字符串
export const queryKeys = {
  profile: ['profile'] as const,

  mcp: {
    list: (params: Record<string, unknown>) => ['mcp', 'servers', params] as const,
    detail: (id: number) => ['mcp', 'servers', id] as const,
    tools: (id: number, params: Record<string, unknown>) => ['mcp', 'servers', id, 'tools', params] as const,
    references: (id: number) => ['mcp', 'servers', id, 'references'] as const,
    prefixes: ['mcp', 'command-prefixes'] as const,
    egress: ['mcp', 'egress'] as const,
    mountable: ['mcp', 'mountable'] as const,
  },

  skills: {
    list: (params: Record<string, unknown>) => ['skills', 'list', params] as const,
    detail: (id: number) => ['skills', 'detail', id] as const,
    references: (id: number) => ['skills', 'detail', id, 'references'] as const,
    options: ['skills', 'options'] as const,
    summary: ['skills', 'summary'] as const,
    mirror: ['skills', 'mirror'] as const,
  },

  agents: {
    list: (params: Record<string, unknown>) => ['agents', 'list', params] as const,
    detail: (id: number) => ['agents', 'detail', id] as const,
    options: ['agents', 'options'] as const,
    summary: ['agents', 'summary'] as const,
    mountOptions: (id: number) => ['agents', 'detail', id, 'mount-options'] as const,
    publishCheck: (id: number) => ['agents', 'detail', id, 'publish-check'] as const,
    versions: (id: number, params: Record<string, unknown>) => ['agents', 'detail', id, 'versions', params] as const,
    version: (id: number, version: number) => ['agents', 'detail', id, 'versions', version] as const,
    versionCompare: (id: number, left: number, right: number) =>
      ['agents', 'detail', id, 'versions', 'compare', left, right] as const,
    /** 跨环境导入申请（单级审批） */
    importRequests: (params: Record<string, unknown>) =>
      ['agents', 'import-requests', params] as const,
    importSummary: ['agents', 'import-requests', 'summary'] as const,
  },

  departments: ['departments'] as const,

  users: {
    root: ['users'] as const,
    list: (params: Record<string, unknown>) => ['users', 'list', params] as const,
    sessions: (id: number) => ['users', 'sessions', id] as const,
  },

  chat: {
    conversations: (params: Record<string, unknown>) => ['chat', 'conversations', params] as const,
    messages: (id: number, params: Record<string, unknown>) => ['chat', 'messages', id, params] as const,
    modelHealth: ['chat', 'model-health'] as const,
    modelConfig: ['chat', 'model-config'] as const,
  },
}
