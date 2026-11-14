import { useQuery } from '@tanstack/react-query'
import { usageApi, usageQueryKeys } from '@/api/usage'
import type { UsageOverviewParams } from '@/api/usage'

export function useUsageOverview(params: UsageOverviewParams, enabled: boolean) {
  return useQuery({
    queryKey: usageQueryKeys.overview(params),
    enabled,
    staleTime: 30_000,
    retry: 0,
    queryFn: () => usageApi.overview(params),
  })
}
