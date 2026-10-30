const missing = "—";
const usdOptions: Intl.NumberFormatOptions = {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 6,
};

export function formatChannelCount(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? missing : new Intl.NumberFormat("en-US").format(value);
}

export function formatChannelUsd(value: number | string | null | undefined): string {
  if (value == null || value === "") return missing;
  const amount = Number(value);
  if (!Number.isFinite(amount)) return missing;
  if (amount > 0 && amount < 0.000001) return "<$0.000001";
  return new Intl.NumberFormat("en-US", usdOptions).format(amount);
}

export function formatChannelDuration(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value < 0) return missing;
  if (value >= 1000) return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value / 1000) + " s";
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(value) + " ms";
}

export function formatChannelPercent(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value)
    ? missing
    : new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value) + "%";
}

export function formatChannelTimestamp(value: string | null | undefined): string {
  if (!value) return missing;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? missing : date.toLocaleString();
}

export function formatChannelConfigState(value: "active" | "provisioning" | "error"): string {
  return { active: "Configured", provisioning: "Provisioning", error: "Setup error" }[value];
}
