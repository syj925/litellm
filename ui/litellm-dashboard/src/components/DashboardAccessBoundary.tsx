"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import useIsOrgAdmin from "@/app/(dashboard)/hooks/useIsOrgAdmin";
import { useTeams } from "@/app/(dashboard)/hooks/teams/useTeams";
import { useUISettings } from "@/app/(dashboard)/hooks/uiSettings/useUISettings";
import LoadingScreen from "@/components/common_components/LoadingScreen";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { canAccessDashboardRoute, isProxyDashboardRole } from "@/utils/dashboardAccess";
import { isUserTeamAdminForAnyTeam } from "@/utils/roles";
import { routeSegmentForPathname, uiHref } from "@/utils/uiHref";

export default function DashboardAccessBoundary({ children }: { children: ReactNode }) {
  const auth = useAuthorized();
  const isOrgAdmin = useIsOrgAdmin();
  const { data: teams, isLoading: teamsLoading } = useTeams();
  const settings = useUISettings();
  const page = routeSegmentForPathname(usePathname());
  const proxyAdmin = isProxyDashboardRole(auth.userRole);
  if (auth.isLoading || !auth.isAuthorized) return <LoadingScreen />;
  const waitingForDelegatedAccess = teamsLoading && ["teams", "projects", "agents", "vector-stores"].includes(page);
  const waitingForSettings = settings.isPending || waitingForDelegatedAccess;
  const isAccountRecovery = page === "change-password";
  if (!proxyAdmin && !isAccountRecovery && waitingForSettings) {
    return <LoadingScreen />;
  }
  const values = settings.data?.values;
  const accessContext = {
    userRole: auth.userRole,
    isViewOnly: auth.isViewOnly,
    isOrgAdmin,
    isTeamAdmin: isUserTeamAdminForAnyTeam(teams ?? null, auth.userId ?? ""),
    enabledPagesInternalUsers: values?.enabled_ui_pages_internal_users ?? null,
    enableProjectsUI: Boolean(values?.enable_projects_ui),
    allowAgentsForTeamAdmins: Boolean(values?.allow_agents_for_team_admins),
    allowVectorStoresForTeamAdmins: Boolean(values?.allow_vector_stores_for_team_admins),
  };
  const allowed = canAccessDashboardRoute(page, accessContext);
  const accessVerified = proxyAdmin || isAccountRecovery || !settings.error;
  if (allowed && accessVerified) return <>{children}</>;
  return <UnavailableDashboardPage verificationFailed={allowed} />;
}

function UnavailableDashboardPage({ verificationFailed }: { verificationFailed: boolean }) {
  return (
    <div className="mx-auto max-w-2xl p-6">
      <Alert variant="error">
        <AlertTitle>{verificationFailed ? "Page access could not be verified" : "Access required"}</AlertTitle>
        <AlertDescription>
          {verificationFailed
            ? "Refresh to verify your account's available pages."
            : "This page is not available to your account. Your personal keys, usage, logs, wallet and billing remain available from the sidebar."}
        </AlertDescription>
      </Alert>
      <Link href={uiHref("home")} className="mt-4 inline-block text-sm font-medium underline">
        Back to Home
      </Link>
    </div>
  );
}
