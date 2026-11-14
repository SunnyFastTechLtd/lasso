/** 后端 StandardPagination 固定 5 键（无 next/previous，不支持 ?ordering） */
export interface Paginated<T> {
  count: number
  page: number
  page_size: number
  total_pages: number
  results: T[]
}

/** 统一错误体 */
export interface ApiErrorBody {
  detail?: string
  code?: number | string
  [field: string]: unknown
}

/** 通用值/标签对 */
export interface Choice<V extends string = string> {
  value: V
  label: string
}

/** 空分页响应（用于「未实现」占位） */
export const emptyPage = <T>(): Paginated<T> => ({
  count: 0,
  page: 1,
  page_size: 20,
  total_pages: 0,
  results: [],
})
