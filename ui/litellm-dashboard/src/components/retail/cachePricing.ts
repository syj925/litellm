export interface CacheTierDraft {
  ttl_seconds: string;
  price: string;
}

export function cacheDurationLabel(seconds: string | number): string {
  const value = Number(seconds);
  if (!Number.isSafeInteger(value) || value <= 0) return "Unspecified duration";
  if (value % 3600 === 0) return `${value / 3600}h`;
  if (value % 60 === 0) return `${value / 60}m`;
  return `${value}s`;
}

export function cacheTierEntries<T>(tiers: Readonly<Record<string, T>> | null | undefined): [string, T][] {
  return Object.entries(tiers ?? {})
    .filter(([ttl]) => /^\d+$/.test(ttl) && Number.isSafeInteger(Number(ttl)) && Number(ttl) > 0)
    .sort(([left], [right]) => Number(left) - Number(right));
}

export function validateCacheTierDrafts(
  drafts: CacheTierDraft[],
): { tiers: Record<string, string>; error: null } | { tiers: null; error: string } {
  if (drafts.length > 16) return { tiers: null, error: "Configure no more than 16 cache durations." };
  const keys = drafts.map(({ ttl_seconds }) => String(Number(ttl_seconds)));
  if (
    drafts.some(
      ({ ttl_seconds }) => !/^\d+$/.test(ttl_seconds) || Number(ttl_seconds) < 1 || Number(ttl_seconds) > 31536000,
    )
  ) {
    return { tiers: null, error: "Cache durations must be whole seconds between 1 and 31536000." };
  }
  if (new Set(keys).size !== keys.length) return { tiers: null, error: "Each cache duration can have only one price." };
  if (drafts.some(({ price }) => !/^\d{1,9}(?:\.\d{1,9})?$/.test(price))) {
    return { tiers: null, error: "Enter a non-negative decimal USD price for every cache duration." };
  }
  return { tiers: Object.fromEntries(drafts.map(({ price }, index) => [keys[index], price])), error: null };
}
