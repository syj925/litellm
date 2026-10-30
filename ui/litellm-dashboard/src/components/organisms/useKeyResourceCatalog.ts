import { useQuery } from "@tanstack/react-query";
import { retailRequest } from "../retail/api";
import type { KeyResourceCatalog } from "./resourceRoutingPolicy";

export function useKeyResourceCatalog({
  accessToken,
  targetUserId,
  admin = false,
}: {
  accessToken: string | null;
  targetUserId: string | null;
  admin?: boolean;
}) {
  return useQuery({
    queryKey: ["retail", "key-resource-pools", targetUserId, admin],
    queryFn: () =>
      retailRequest<KeyResourceCatalog>(
        admin ? `/admin/pools/catalog?user_id=${encodeURIComponent(targetUserId ?? "")}` : "/pools",
        accessToken,
      ),
    enabled: Boolean(accessToken && targetUserId),
    retry: false,
  });
}
