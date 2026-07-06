export const usd = (n: number | null | undefined) =>
  `$${(n ?? 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const pct = (num: number, den: number) =>
  den <= 0 ? 0 : Math.round((num / den) * 100);

export const num = (n: number | null | undefined) =>
  (n ?? 0).toLocaleString("en-US");

/** Short label for a run id like "web-1a2b3c4d" → "1a2b3c4d". */
export const shortRun = (id: string | null | undefined) =>
  (id ?? "").replace(/^web-/, "");
