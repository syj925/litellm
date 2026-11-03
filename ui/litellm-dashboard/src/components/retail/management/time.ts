export function beijingTimeInput(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Date(date.getTime() + 8 * 60 * 60 * 1000).toISOString().slice(0, 16);
}

type ReportInterval = { start_at: string; end_at: string };
export type IntervalResult = { ok: true; interval: ReportInterval | null } | { ok: false; error: string };

export function managementInterval(from: string, until: string, required = false): IntervalResult {
  if (!from && !until && !required) return { ok: true, interval: null };
  if (!from || !until) return { ok: false, error: "Choose both a start and an end time." };
  const pattern = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;
  if (!pattern.test(from) || !pattern.test(until)) return { ok: false, error: "Enter valid dates and times." };
  const start = new Date(`${from}:00+08:00`);
  const end = new Date(`${until}:00+08:00`);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
    return { ok: false, error: "Enter valid dates and times." };
  }
  if (beijingTimeInput(start.toISOString()) !== from || beijingTimeInput(end.toISOString()) !== until) {
    return { ok: false, error: "Enter valid dates and times." };
  }
  const duration = end.getTime() - start.getTime();
  if (duration <= 0) return { ok: false, error: "The end time must be after the start time." };
  if (duration > 31 * 24 * 60 * 60 * 1000) return { ok: false, error: "Choose a window of 31 days or less." };
  return { ok: true, interval: { start_at: start.toISOString(), end_at: end.toISOString() } };
}
