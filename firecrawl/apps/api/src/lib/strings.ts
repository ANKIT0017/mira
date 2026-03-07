import { isSelfHosted } from "./deployment";

export const BLOCKLISTED_URL_MESSAGE = isSelfHosted()
  ? "This website has been blocklisted and cannot be scraped. Websites may be blocklisted due to: (1) Terms of service restrictions, (2) Legal requirements, (3) Technical limitations that prevent reliable scraping, or (4) Site owner requests. Please check your server configuration and logs for more details about why this specific domain is blocked."
  : "This website has been blocklisted and cannot be scraped. Websites may be blocklisted due to: (1) Terms of service restrictions, (2) Legal requirements, (3) Technical limitations that prevent reliable scraping, or (4) Site owner requests. If you are part of an enterprise plan and believe this site should be accessible for your use case, please reach out to help@firecrawl.com to discuss the possibility of getting it activated on your account.";
