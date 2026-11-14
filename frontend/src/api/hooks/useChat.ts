import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { chatApi } from '@/api/chat'
import { queryKeys } from '@/api/queryKeys'

/** 会话列表：默认取最近 50 条，足够侧栏滚一轮 */
export function useConversations() {
  return useQuery({
    queryKey: queryKeys.chat.conversations({ page: 1, page_size: 50 }),
    queryFn: () => chatApi.listConversations({ page: 1, page_size: 50 }),
  })
}

/** 历史消息：单会话一次拉满 200 条（对话量小，分页留待后续） */
export function useMessages(conversationId: number | null) {
  return useQuery({
    queryKey: queryKeys.chat.messages(conversationId ?? 0, { page: 1, page_size: 200 }),
    enabled: conversationId !== null,
    queryFn: () => chatApi.messages(conversationId as number, { page: 1, page_size: 200 }),
  })
}

export function useRemoveConversation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => chatApi.removeConversation(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['chat'] }),
  })
}

/** P1-c：切换助手总开关（默认关闭，关闭时不展示入口，行为与旧版一致） */
export function useAgentSwitchPolicy() {
  return useQuery({
    queryKey: ['chat', 'agent-switch-policy'],
    staleTime: 60_000,
    queryFn: () => chatApi.agentSwitchPolicy(),
  })
}

/** P1-c：切换助手 → 后端新建会话承接历史，返回新会话 */
export function useSwitchAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, agentId }: { id: number; agentId: number }) =>
      chatApi.switchAgent(id, agentId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['chat'] }),
  })
}

/** 模型端点健康：驱动顶部状态标签与"发送前就知道会失败"的提示 */
export function useModelHealth() {
  return useQuery({
    queryKey: queryKeys.chat.modelHealth,
    staleTime: 30_000,
    queryFn: () => chatApi.modelHealth(),
  })
}

export function useModelConfig() {
  return useQuery({
    queryKey: queryKeys.chat.modelConfig,
    staleTime: 60_000,
    queryFn: () => chatApi.modelConfig(),
  })
}
