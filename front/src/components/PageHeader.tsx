export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between mb-gutter gap-4">
      <div>
        <h1 className="font-headline-lg text-headline-lg text-primary mb-1">{title}</h1>
        {subtitle && (
          <p className="font-body-md text-body-md text-on-surface-variant">{subtitle}</p>
        )}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}
