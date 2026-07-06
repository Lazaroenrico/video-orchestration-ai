import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Icon } from "./Icon";

type Variant = "primary" | "secondary" | "ghost" | "danger";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-primary text-on-primary hover:bg-surface-tint",
  secondary:
    "bg-surface-container-lowest text-primary border border-surface-border hover:bg-surface-container-low",
  ghost: "text-on-surface-variant hover:bg-surface-container-low",
  danger: "bg-error/10 text-error hover:bg-error/20",
};

export function Button({
  children,
  variant = "primary",
  icon,
  className = "",
  ...rest
}: {
  children: ReactNode;
  variant?: Variant;
  icon?: string;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={`inline-flex items-center justify-center gap-2 px-4 py-2 rounded-lg font-label-md text-label-md font-bold transition-colors disabled:opacity-50 disabled:pointer-events-none ${VARIANTS[variant]} ${className}`}
      {...rest}
    >
      {icon && <Icon name={icon} size={18} />}
      {children}
    </button>
  );
}
