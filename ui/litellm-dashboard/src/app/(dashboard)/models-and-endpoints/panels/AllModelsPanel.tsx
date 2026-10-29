"use client";

import AllModelsTab from "@/app/(dashboard)/models-and-endpoints/components/AllModelsTab";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { isProxyAdminRole } from "@/utils/roles";
import { uiHref } from "@/utils/uiHref";
import { ChannelIdentityProvider } from "@/components/retail/channels/DeploymentChannelIdentity";
import { ALL_MODEL_GROUPS_VALUE } from "@/app/(dashboard)/models-and-endpoints/components/AllModelsTable";
import { useModelDashboardData } from "@/app/(dashboard)/models-and-endpoints/useModelDashboardData";
import {
  useModelDetailRouting,
  useModelGroupFilterRouting,
} from "@/app/(dashboard)/models-and-endpoints/detailNavigation";

export default function AllModelsPanel() {
  const { userRole } = useAuthorized();
  const { modelGroup, setModelGroup } = useModelGroupFilterRouting();
  const { availableModelGroups, availableModelAccessGroups } = useModelDashboardData();
  const { openModel, openTeam } = useModelDetailRouting();

  return (
    <ChannelIdentityProvider>
      {isProxyAdminRole(userRole) && (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-muted/20 p-3">
          <p className="text-sm text-muted-foreground">Group models by a named upstream channel and resource pool.</p>
          <Button variant="outline" size="sm" render={<Link href={uiHref("channels")} />}>
            Manage channels
          </Button>
        </div>
      )}
      <AllModelsTab
        selectedModelGroup={modelGroup}
        setSelectedModelGroup={(group) => setModelGroup(group === ALL_MODEL_GROUPS_VALUE ? null : group)}
        availableModelGroups={availableModelGroups}
        availableModelAccessGroups={availableModelAccessGroups}
        setSelectedModelId={openModel}
        setSelectedTeamId={openTeam}
      />
    </ChannelIdentityProvider>
  );
}
