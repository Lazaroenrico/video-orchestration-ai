import { useState } from "react";
import { PageHeader } from "../components/PageHeader";
import { Card, SectionTitle } from "../components/Card";
import { Icon } from "../components/Icon";
import { Button } from "../components/Button";
import { RoleBadge } from "../components/RoleBadge";
import { useNavigate } from "react-router";
import {
  useCreators,
  usePrompts,
  useSessionQuery,
  useMembersQuery,
  useUpdateMemberRoleMutation,
  useRevokeMemberMutation,
  useInvitationsQuery,
  useCreateInvitationMutation,
  useCancelInvitationMutation,
} from "../api/queries";
import type { MemberRecord, UserRole, Invitation } from "../api/contracts";

function StoreRow({ label, path, exists }: { label: string; path?: string; exists?: boolean }) {
  return (
    <div className="flex items-center justify-between py-3 border-b border-surface-border last:border-0">
      <div>
        <div className="font-body-md text-body-md text-primary">{label}</div>
        <div className="font-mono text-label-sm text-label-sm text-on-surface-variant break-all">
          {path ?? "—"}
        </div>
      </div>
      <span
        className={`inline-flex items-center gap-1 font-label-sm text-label-sm ${
          exists ? "text-success-published" : "text-on-surface-variant"
        }`}
      >
        <Icon name={exists ? "check_circle" : "remove_circle_outline"} size={16} />
        {exists ? "Present" : "Not created"}
      </span>
    </div>
  );
}

function MembersManager() {
  const session = useSessionQuery();
  const user = session.data;
  const canReadMembers = user ? user.permissions.includes("members:read") : false;
  const canWriteMembers = user ? user.permissions.includes("members:write") : false;

  const membersQuery = useMembersQuery(canReadMembers);
  const invitationsQuery = useInvitationsQuery(canReadMembers);
  const updateMutation = useUpdateMemberRoleMutation();
  const revokeMutation = useRevokeMemberMutation();
  const createInviteMutation = useCreateInvitationMutation();
  const cancelInviteMutation = useCancelInvitationMutation();

  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<UserRole>("member");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [showAddForm, setShowAddForm] = useState(false);

  if (!canReadMembers) {
    return (
      <Card className="mb-gutter">
        <SectionTitle title="Organization Members" />
        <div className="flex items-center gap-3 py-3 text-on-surface-variant font-body-md text-body-md">
          <Icon name="lock" size={20} />
          <span>
            Member management is restricted to organization Admins and Owners.
          </span>
        </div>
      </Card>
    );
  }

  const isOwner = user?.role === "owner";
  const members = membersQuery.data?.members ?? [];
  const invitations = invitationsQuery.data?.invitations ?? [];

  const handleInvite = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!inviteEmail.trim()) return;
    setErrorMsg(null);
    try {
      await createInviteMutation.mutateAsync({
        email: inviteEmail.trim(),
        role: inviteRole,
      });
      setInviteEmail("");
      setInviteRole("member");
      setShowAddForm(false);
    } catch (err: any) {
      setErrorMsg(err.detail || err.message || "Failed to invite member");
    }
  };

  const handleRoleChange = async (member: MemberRecord, targetRole: UserRole) => {
    setErrorMsg(null);
    try {
      await updateMutation.mutateAsync({
        subject: member.subject,
        role: targetRole,
      });
    } catch (err: any) {
      setErrorMsg(err.detail || err.message || "Failed to update member role");
    }
  };

  const handleRevoke = async (member: MemberRecord) => {
    if (!confirm(`Are you sure you want to remove ${member.display_name || member.email || member.subject}?`)) {
      return;
    }
    setErrorMsg(null);
    try {
      await revokeMutation.mutateAsync(member.subject);
    } catch (err: any) {
      setErrorMsg(err.detail || err.message || "Failed to remove member");
    }
  };

  const handleCancelInvite = async (invitation: Invitation) => {
    if (!confirm(`Cancel invitation for ${invitation.email}?`)) {
      return;
    }
    setErrorMsg(null);
    try {
      await cancelInviteMutation.mutateAsync(invitation.email);
    } catch (err: any) {
      setErrorMsg(err.detail || err.message || "Failed to cancel invitation");
    }
  };

  return (
    <Card className="mb-gutter">
      <div className="flex items-center justify-between mb-4">
        <div>
          <SectionTitle title="Organization Members" />
          <p className="font-label-sm text-label-sm text-on-surface-variant">
            {members.length} {members.length === 1 ? "member" : "members"} in {user?.organization.name}
          </p>
        </div>
        {canWriteMembers && !showAddForm && (
          <Button
            variant="secondary"
            icon="person_add"
            onClick={() => setShowAddForm(true)}
          >
            Invite Member
          </Button>
        )}
      </div>

      {errorMsg && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-error-border bg-error/10 p-3 text-label-md text-error-text">
          <Icon name="error" size={18} />
          <span>{errorMsg}</span>
        </div>
      )}

      {showAddForm && (
        <form onSubmit={handleInvite} className="mb-6 rounded-lg border border-surface-border bg-surface-container p-4">
          <div className="flex items-center justify-between mb-3">
            <span className="font-label-md text-label-md font-bold text-primary">Invite Organization Member</span>
            <button
              type="button"
              onClick={() => setShowAddForm(false)}
              className="text-on-surface-variant hover:text-primary"
            >
              <Icon name="close" size={18} />
            </button>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
            <div>
              <label className="block text-label-sm text-on-surface-variant mb-1">Email Address *</label>
              <input
                type="email"
                required
                placeholder="user@example.com"
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                className="w-full rounded-md border border-surface-border bg-surface px-3 py-2 text-body-md text-primary"
              />
            </div>
            <div>
              <label className="block text-label-sm text-on-surface-variant mb-1">Role *</label>
              <select
                value={inviteRole}
                onChange={(e) => setInviteRole(e.target.value as UserRole)}
                className="w-full rounded-md border border-surface-border bg-surface px-3 py-2 text-body-md text-primary"
              >
                {isOwner && <option value="owner">Owner (Full admin & ownership)</option>}
                {isOwner && <option value="admin">Admin (Manage members & campaigns)</option>}
                <option value="member">Member (Create & review campaigns)</option>
                <option value="viewer">Viewer (Read-only access)</option>
              </select>
            </div>
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setShowAddForm(false)}>
              Cancel
            </Button>
            <Button variant="primary" type="submit" loading={createInviteMutation.isPending}>
              Send Invitation
            </Button>
          </div>
        </form>
      )}

      {/* Pending Invitations Section */}
      {invitations.length > 0 && (
        <div className="mb-6 rounded-lg border border-surface-border bg-surface-container-low/50 p-4">
          <div className="font-label-md text-label-md font-bold text-primary mb-2 flex items-center gap-2">
            <Icon name="mail" size={16} />
            <span>Pending Invitations ({invitations.length})</span>
          </div>
          <div className="divide-y divide-surface-border">
            {invitations.map((inv) => {
              const canCancelThis =
                canWriteMembers &&
                (isOwner || (inv.role === "member" || inv.role === "viewer"));

              return (
                <div key={inv.email} className="flex items-center justify-between py-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-body-md text-body-md font-medium text-primary truncate">
                        {inv.email}
                      </span>
                      <RoleBadge role={inv.role} />
                    </div>
                    <div className="text-label-sm text-on-surface-variant">
                      Invited {new Date(inv.created_at).toLocaleDateString()}
                    </div>
                  </div>
                  {canCancelThis && (
                    <Button
                      variant="ghost"
                      icon="cancel"
                      loading={cancelInviteMutation.isPending}
                      onClick={() => handleCancelInvite(inv)}
                    >
                      Cancel
                    </Button>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Active Members Section */}
      <div className="font-label-md text-label-md font-bold text-primary mb-2">
        Active Members
      </div>

      {membersQuery.isLoading && (
        <div className="py-6 text-center text-on-surface-variant font-label-md">
          Loading organization members...
        </div>
      )}

      {membersQuery.isError && (
        <div className="py-6 text-center text-error-text font-label-md">
          Failed to load organization members: {membersQuery.error?.message || "Unknown error"}
        </div>
      )}

      {membersQuery.isSuccess && members.length === 0 && (
        <div className="py-6 text-center text-on-surface-variant font-label-md">
          No members found in this organization.
        </div>
      )}

      {membersQuery.isSuccess && members.length > 0 && (
        <div className="divide-y divide-surface-border">
          {members.map((member) => {
            const canManageTarget =
              canWriteMembers &&
              (isOwner || (member.role === "member" || member.role === "viewer"));

            return (
              <div key={member.id} className="flex items-center justify-between py-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-body-md text-body-md font-medium text-primary truncate">
                      {member.display_name || member.email || member.subject}
                    </span>
                    <RoleBadge role={member.role} />
                  </div>
                  <div className="font-mono text-label-sm text-on-surface-variant truncate">
                    {member.subject} {member.email && member.display_name ? `· ${member.email}` : ""}
                  </div>
                </div>

                {canManageTarget ? (
                  <div className="flex items-center gap-2">
                    <select
                      value={member.role}
                      onChange={(e) => handleRoleChange(member, e.target.value as UserRole)}
                      disabled={updateMutation.isPending}
                      className="rounded border border-surface-border bg-surface-container-low px-2 py-1 text-label-sm text-primary"
                    >
                      {isOwner && <option value="owner">owner</option>}
                      {isOwner && <option value="admin">admin</option>}
                      <option value="member">member</option>
                      <option value="viewer">viewer</option>
                    </select>
                    <button
                      type="button"
                      title="Revoke Membership"
                      onClick={() => handleRevoke(member)}
                      disabled={revokeMutation.isPending}
                      className="inline-flex h-8 w-8 items-center justify-center rounded text-on-surface-variant hover:text-error-text hover:bg-error/10 transition-colors"
                    >
                      <Icon name="delete" size={18} />
                    </button>
                  </div>
                ) : (
                  <div className="text-label-sm text-on-surface-variant font-medium uppercase tracking-wider">
                    {member.role}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

export function Settings() {
  const prompts = usePrompts();
  const creators = useCreators();
  const navigate = useNavigate();

  return (
    <div>
      <PageHeader
        title="Workspace Configuration"
        subtitle="Inspect persisted local stores, manage organization members, and review launch defaults."
      />

      <div className="grid grid-cols-12 gap-gutter">
        <div className="col-span-12 lg:col-span-7">
          <MembersManager />

          <Card className="mb-gutter">
            <SectionTitle title="Local Stores" />
            <StoreRow
              label="Prompt templates"
              path={prompts.data?.store_path}
              exists={prompts.data?.exists}
            />
            <StoreRow
              label="Creators library"
              path={creators.data?.store_path}
              exists={creators.data?.exists}
            />
          </Card>

          <Card>
            <SectionTitle title="Frontend launch defaults" />
            {[
              ["Default platform", "TikTok"],
              ["Default batch size", "6"],
              ["Creative review", "One combined review"],
              ["Dry-run mode", "Config-driven (providers.yaml)"],
            ].map(([k, v]) => (
              <div key={k} className="flex items-center justify-between py-3 border-b border-surface-border last:border-0">
                <span className="font-body-md text-body-md text-on-surface-variant">{k}</span>
                <span className="font-body-md text-body-md text-primary font-medium">{v}</span>
              </div>
            ))}
            <p className="mt-3 font-label-sm text-label-sm text-on-surface-variant">
              These are the defaults shown when starting a campaign. Change them per run in the launch wizard; this screen does not persist preferences.
            </p>
          </Card>
        </div>

        <div className="col-span-12 lg:col-span-5">
          <Card className="bg-ai-processing/5 border-ai-processing/20">
            <div className="flex items-center gap-2 mb-2 text-ai-processing">
              <Icon name="workspaces" />
              <span className="font-headline-md text-headline-md">Pro Workspace</span>
            </div>
            <p className="font-body-md text-body-md text-on-surface-variant mb-4">
              Marketing Suite · AI UGC Orchestrator. Runtime adapters are managed in the integrations view.
            </p>
            <Button variant="secondary" icon="extension" className="w-full" onClick={() => navigate("/integrations")}>
              Manage Integrations
            </Button>
          </Card>
        </div>
      </div>
    </div>
  );
}
