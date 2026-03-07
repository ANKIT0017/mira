import { TeamFlags } from "../controllers/v2/types";

type LocationOptions = { country?: string };

interface APIRequest {
  zeroDataRetention?: boolean;
  location?: LocationOptions;
  scrapeOptions?: {
    location?: LocationOptions;
  };
}

const SUPPORT_EMAIL = "support@firecrawl.com";

export function checkPermissions(
  request: APIRequest,
  flags?: TeamFlags,
): { error?: string } {
  // zdr perms
  if (request.zeroDataRetention && !flags?.allowZDR) {
    return {
      error: `Zero Data Retention (ZDR) is not enabled for your team. Contact ${SUPPORT_EMAIL} to enable this feature.`,
    };
  }

  // ip whitelist perms
  const needsWhitelist =
    request.location?.country === "us-whitelist" ||
    request.scrapeOptions?.location?.country === "us-whitelist";

  if (needsWhitelist && !flags?.ipWhitelist) {
    return {
      error: `Static IP addresses are not enabled for your team. Contact ${SUPPORT_EMAIL} to get a dedicated set of IP addresses you can whitelist.`,
    };
  }

  return {};
}
