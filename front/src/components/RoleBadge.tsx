import type { UserRole } from "../api/contracts";

export function getRoleBadgeClass(role?: string | UserRole): string {
  switch (role) {
    case "owner":
      return "bg-amber-500/10 text-amber-500 border-amber-500/20";
    case "admin":
      return "bg-purple-500/10 text-purple-400 border-purple-500/20";
    case "member":
      return "bg-blue-500/10 text-blue-400 border-blue-500/20";
    case "viewer":
    default:
      return "bg-surface-container-high text-on-surface-variant border-surface-border";
  }
}

export function RoleBadge({
  role,
  className = "",
}: {
  role?: string | UserRole;
  className?: string;
}) {
  const badgeClass = getRoleBadgeClass(role);
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 font-label-sm text-label-sm font-semibold capitalize ${badgeClass} ${className}`}
    >
      {role ?? "viewer"}
    </span>
  );
}
