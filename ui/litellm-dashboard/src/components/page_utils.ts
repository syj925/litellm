/**
 * Utility functions for working with navigation pages
 */

import { labelText, menuGroups, routeOf, type MenuItem } from "./leftnav";
import { pageDescriptions, PageMetadata } from "./page_metadata";
import { canAccessDashboardRoute } from "@/utils/dashboardAccess";

/**
 * Check if a page is accessible to internal users
 * Include delegated team and organization pages that settings may enable.
 */
const delegatedUserContext = {
  userRole: "Internal User",
  isViewOnly: false,
  isTeamAdmin: true,
  isOrgAdmin: true,
  enableProjectsUI: true,
  allowAgentsForTeamAdmins: true,
  allowVectorStoresForTeamAdmins: true,
};
const isPageAccessibleToInternalUsers = (route: string): boolean =>
  canAccessDashboardRoute(route, delegatedUserContext);

const metadataFor = (item: MenuItem, group: string): PageMetadata => ({
  page: item.page,
  label: labelText(item),
  group,
  description: pageDescriptions[item.page] || "No description available",
});

/**
 * Get all available pages from the navigation menu configuration
 * Used by UI Settings to display available pages for visibility control
 *
 * IMPORTANT: Only returns pages that internal users can access.
 * Pages restricted to admin-only roles are excluded because internal users
 * cannot see them regardless of the UI visibility setting.
 */
export const getAvailablePages = (): PageMetadata[] => {
  const pages: PageMetadata[] = [];

  menuGroups.forEach((group) => {
    group.items.forEach((item) => {
      // Add top-level items (skip parent containers like 'tools', 'experimental', 'settings')
      // Also skip items that internal users cannot access
      if (!item.children && isPageAccessibleToInternalUsers(routeOf(item))) {
        pages.push(metadataFor(item, group.groupLabel));
      }

      // Add children items (also skip those internal users cannot access)
      if (item.children) {
        const parentLabel = labelText(item);
        item.children.forEach((child) => {
          // Include if internal users can access
          if (isPageAccessibleToInternalUsers(routeOf(child))) {
            pages.push(metadataFor(child, `${group.groupLabel} > ${parentLabel}`));
          }
        });
      }
    });
  });

  return pages;
};
