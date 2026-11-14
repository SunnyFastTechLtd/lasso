import { create } from 'zustand'

/** 外壳状态栏的「对话」计数：当前打开会话的提问轮次，由对话页同步 */
interface ChatStatusState {
  conversationId: number | null
  rounds: number
  sync: (conversationId: number | null, rounds: number) => void
}

export const useChatStatusStore = create<ChatStatusState>((set) => ({
  conversationId: null,
  rounds: 0,
  sync: (conversationId, rounds) => set({ conversationId, rounds }),
}))
