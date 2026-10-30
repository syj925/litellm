import { parseAsNumberLiteral, parseAsString, parseAsStringLiteral } from "nuqs";
import { uiHref } from "@/utils/uiHref";

export const channelHours = [1, 6, 24, 168, 720] as const;
export type ChannelHours = (typeof channelHours)[number];
export const channelDetailTabs = ["overview", "models", "errors"] as const;
export type ChannelDetailTab = (typeof channelDetailTabs)[number];

export const channelHoursParser = parseAsNumberLiteral(channelHours).withDefault(24);
export const channelDetailParsers = {
  channel: parseAsString,
  hours: channelHoursParser,
  tab: parseAsStringLiteral(channelDetailTabs).withDefault("overview"),
};

export const channelHourLabels: Readonly<Record<ChannelHours, string>> = {
  1: "Last hour",
  6: "Last 6 hours",
  24: "Last 24 hours",
  168: "Last 7 days",
  720: "Last 30 days",
};

export function channelDetailHref(channelId: string, hours: ChannelHours = 24): string {
  return uiHref("channel-details") + "?channel=" + encodeURIComponent(channelId) + "&hours=" + hours;
}
