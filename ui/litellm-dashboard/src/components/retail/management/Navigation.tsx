import { ScrollText } from "lucide-react";
import type { MenuItem, MenuVisibilityContext } from "@/components/leftnav";
import type { RetailConfig } from "../api";

export const requestPolicyMenu: MenuItem = {
  key: "request-policies",
  page: "request-policies",
  label: "Request Policies",
  icon: <ScrollText className="size-4" />,
  roles: ["Admin", "proxy_admin"],
};
const featurePages = {
  reports: "retailReportsEnabled",
  "operation-audit": "retailAuditEnabled",
  "business-labels": "retailObjectMetadataEnabled",
  "request-policies": "retailRequestPoliciesEnabled",
} as const;
export function retailFeatureVisible(key: string, context: MenuVisibilityContext): boolean {
  if (!Object.hasOwn(featurePages, key)) return true;
  return context[featurePages[key as keyof typeof featurePages]] === true;
}
export function requestPoliciesEnabled(config: RetailConfig | undefined, error: Error | null): boolean {
  return !error && config?.request_policies_enabled === true;
}
