// Material Symbols Outlined glyph (loaded via Google Fonts in index.html).
export function Icon({
  name,
  fill,
  size,
  className = "",
  style,
}: {
  name: string;
  fill?: boolean;
  size?: number;
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <span
      className={`material-symbols-outlined ${className}`}
      style={{
        ...(size ? { fontSize: `${size}px` } : {}),
        ...(fill ? { fontVariationSettings: "'FILL' 1" } : {}),
        ...style,
      }}
    >
      {name}
    </span>
  );
}
