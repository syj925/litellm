"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { createContext, useContext, type ReactNode } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { isProxyAdminRole } from "@/utils/roles";
import { uiHref } from "@/utils/uiHref";
import { retailRequest } from "../api";
import type { ChannelCatalog } from "./types";

const ChannelContext = createContext<ChannelCatalog | null>(null);

export function ChannelIdentityProvider({ children }: { children: ReactNode }) {
  const { accessToken, userRole } = useAuthorized();
  const catalog = useQuery({
    queryKey: ["retail", "channels"],
    queryFn: () => retailRequest<ChannelCatalog>("/admin/channels", accessToken),
    enabled: Boolean(accessToken) && isProxyAdminRole(userRole),
    retry: false,
    staleTime: 30_000,
  });
  return (
    <ChannelContext.Provider
      value={accessToken && isProxyAdminRole(userRole) && catalog.data?.enabled ? catalog.data : null}
    >
      {children}
    </ChannelContext.Provider>
  );
}

export default function DeploymentChannelIdentity({ deploymentId }: { deploymentId: string }) {
  const catalog = useContext(ChannelContext);
  if (!catalog) return null;
  const channel = catalog.items.find((item) => item.deployment_ids.includes(deploymentId));
  return (
    <Link
      href={uiHref("channels")}
      title={
        channel ? `${channel.name} · ${channel.pool_name}` : "Name this upstream connection and assign a resource pool"
      }
      className="max-w-64 truncate text-xs text-info hover:underline"
    >
      {channel ? `${channel.name} · ${channel.pool_name}` : "Unassigned channel"}
    </Link>
  );
}
