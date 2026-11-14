import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from './request'
import { queryKeys } from './queryKeys'
import type { Paginated } from '@/types/common'

export interface Department {
  id: number
  name: string
  description: string
  created_at: string
}

export interface DepartmentPayload {
  name?: string
  description?: string
}

/** 部门清单（仅管理员可读，供可见范围选择与管理页） */
export const departmentsApi = {
  list: () => http.get<Paginated<Department> | Department[]>('/auth/departments/', { page_size: 100 }),
  create: (payload: DepartmentPayload) => http.post<Department>('/auth/departments/', payload),
  update: (id: number, payload: DepartmentPayload) =>
    http.patch<Department>(`/auth/departments/${id}/`, payload),
}

export function useDepartments(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.departments,
    enabled,
    staleTime: 120_000,
    queryFn: async () => {
      const res = await departmentsApi.list()
      return Array.isArray(res) ? res : res.results
    },
  })
}

function useInvalidateDepartments() {
  const queryClient = useQueryClient()
  return () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.departments }),
      // 重命名会同步 User.department，用户列表需要重新取
      queryClient.invalidateQueries({ queryKey: queryKeys.users.root }),
    ])
}

export function useCreateDepartment() {
  const invalidate = useInvalidateDepartments()
  return useMutation({ mutationFn: departmentsApi.create, onSuccess: invalidate })
}

export function useUpdateDepartment() {
  const invalidate = useInvalidateDepartments()
  return useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: DepartmentPayload }) =>
      departmentsApi.update(id, payload),
    onSuccess: invalidate,
  })
}
