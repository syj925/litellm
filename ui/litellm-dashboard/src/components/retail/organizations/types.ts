export interface RetailOrganization {
  id: string;
  name: string;
  enabled: boolean;
  version: number;
  created_at: string;
  updated_at: string;
  member_role: "admin" | "member" | null;
  can_edit: boolean;
  can_associate_members: boolean;
  can_manage_members: boolean;
  can_view_events: boolean;
}

export interface RetailOrganizationMember {
  has_finance_grant?: boolean;
  user_id: string;
  role: "admin" | "member";
  enabled: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface OrganizationMemberResult {
  organization: RetailOrganization;
  member: RetailOrganizationMember;
}
