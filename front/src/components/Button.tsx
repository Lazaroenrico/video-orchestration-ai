import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Icon } from "./Icon";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Feedback = "idle" | "success" | "error";

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
  loading = false,
  feedback = "idle",
  className = "",
  disabled,
  ...rest
}: {
  children: ReactNode;
  variant?: Variant;
  icon?: string;
  loading?: boolean;
  feedback?: Feedback;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  const feedbackClass =
    feedback === "success"
      ? "bg-success-published text-on-primary"
      : feedback === "error"
      ? "bg-error text-on-error"
      : "";
  const visibleIcon = loading ? "progress_activity" : feedback === "success" ? "check" : feedback === "error" ? "error" : icon;
  return (
    <button
      className={`inline-flex min-h-11 items-center justify-center gap-2 whitespace-nowrap rounded-lg px-4 py-2 font-label-md text-label-md font-bold transition-[background-color,color,transform] duration-150 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-secondary active:translate-y-px disabled:pointer-events-none disabled:opacity-50 ${VARIANTS[variant]} ${feedbackClass} ${className}`}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {visibleIcon && <Icon name={visibleIcon} size={18} className={loading ? "animate-spin" : ""} />}
      {children}
    </button>
  );
}
