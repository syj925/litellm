import { useQuery } from "@tanstack/react-query";
import { createApiClient, type RequestOptions } from "@/lib/http/client";
import { oidcStatusSchema } from "./oidcLoginInput";

export function useOrganizationOidcStatus(base: string) {
  const options = {
    queryKey: ["retail", "auth", "oidc", "status", base],
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      const client = createApiClient({ getBaseUrl: () => base });
      const request: RequestOptions = {
        signal,
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
      };
      return oidcStatusSchema.parse(await client.get<unknown>("/retail/auth/oidc/status", request));
    },
    retry: false,
    staleTime: 0,
    gcTime: 0,
    refetchOnMount: "always" as const,
    refetchOnWindowFocus: "always" as const,
  };
  return useQuery(options);
}
