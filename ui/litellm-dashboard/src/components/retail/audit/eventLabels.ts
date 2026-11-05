export const organizationEventLabels: Record<string, string> = {
  "organization.created": "Organization created",
  "organization.updated": "Organization updated",
  "organization.member_associated": "Account associated",
  "organization.member_updated": "Membership updated",
};

export const eventLabels: Record<string, string> = {
  ...organizationEventLabels,
  "report.created": "Report requested",
  "report.completed": "Report completed",
  "report.failed": "Report failed",
  "object_metadata.updated": "Business labels updated",
  "model_budget.created": "Model budget created",
  "model_budget.updated": "Model budget updated",
  "model_budget.scheduled": "Next model budget window scheduled",
  "monthly_budget.created": "Monthly budget plan created",
  "monthly_budget.updated": "Monthly budget plan updated",
  "monthly_budget.stopped": "Monthly budget plan stopped",
  "billing_organization.updated": "Billing organization updated",
  "organization_report.created": "Organization report requested",
  "organization_report.completed": "Organization report completed",
  "organization_report.failed": "Organization report failed",
  "finance_grant.updated": "Finance access updated",
  "delegated_report.created": "Delegated finance report requested",
  "delegated_report.completed": "Delegated finance report completed",
  "delegated_report.failed": "Delegated finance report failed",
  "native_key.rotated": "API key rotated",
  "native_password.changed": "Account password changed",
  "native_session.revoked": "Account sessions revoked",
  "native_identity.configured": "Organization sign-in bindings configured",
  "native_identity.signed_in": "Organization sign-in completed",
  "native_api_identity.configured": "API access-token bindings configured",
  "audit_delivery.updated": "Audit delivery configuration updated",
  "audit_delivery.retried": "Audit delivery retried",
  "audit_delivery.discarded": "Outstanding audit deliveries discarded",
};

export function authorizedAuditAction(action: string, admin: boolean): string {
  const labels = admin ? eventLabels : organizationEventLabels;
  return Object.hasOwn(labels, action) ? action : "";
}
