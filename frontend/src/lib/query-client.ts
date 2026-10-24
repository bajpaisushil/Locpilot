import { QueryClient } from '@tanstack/react-query'
import { ApiError } from './api'

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Client errors (4xx) are final; transient failures get a couple of retries.
        retry: (count, err) => !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
        refetchOnWindowFocus: false,
        staleTime: 2_000,
      },
      mutations: { retry: false },
    },
  })
}
