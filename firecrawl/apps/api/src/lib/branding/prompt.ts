import { BrandingProfile } from "../../types/branding";
import { ButtonSnapshot, BrandingLLMInput } from "./types";
import { parse, rgb } from "culori";

export function buildBrandingPrompt(input: BrandingLLMInput): string {
  const {
    jsAnalysis,
    buttons,
    logoCandidates,
    brandName,
    backgroundCandidates,
    url,
  } = input;

  let prompt = `Analyze the branding of this website: ${url}\n\n`;

  // Add JS analysis context
  prompt += `## JavaScript Analysis (Baseline):\n`;
  prompt += `Color Scheme: ${jsAnalysis.colorScheme || "unknown"}\n`;

  if (jsAnalysis.colors) {
    prompt += `Detected Colors:\n`;
    Object.entries(jsAnalysis.colors).forEach(([key, value]) => {
      if (value) prompt += `- ${key}: ${value}\n`;
    });
  }

  if (jsAnalysis.fonts && jsAnalysis.fonts.length > 0) {
    prompt += `\nRaw Fonts (need cleaning):\n`;
    jsAnalysis.fonts.forEach((font: any) => {
      const family = typeof font === "string" ? font : font.family;
      const count = typeof font === "object" && font.count ? font.count : "";
      prompt += `- ${family}${count ? ` (used ${count}x)` : ""}\n`;
    });
    prompt += `\n**FONT CLEANING INSTRUCTIONS:**\n`;
    prompt += `- Remove obfuscated names (e.g., "__suisse_6d5c28" → "Suisse", "__Roboto_Mono_c8ca7d" → "Roboto Mono")\n`;
    prompt += `- Skip fallback fonts (e.g., "__suisse_Fallback_6d5c28" → ignore)\n`;
    prompt += `- Skip CSS variables (e.g., "var(--font-sans)" → ignore)\n`;
    prompt += `- Skip generic fonts (e.g., "system-ui", "sans-serif", "ui-sans-serif" → ignore)\n`;
    prompt += `- Keep only real, meaningful brand fonts (max 5)\n`;
    prompt += `- Assign roles based on usage: heading, body, monospace, display\n\n`;
  }

  // Helper to analyze color vibrancy
  const getColorInfo = (colorStr: string) => {
    if (!colorStr || colorStr === "transparent")
      return { isVibrant: false, description: "transparent" };

    let r = 0,
      g = 0,
      b = 0;
    try {
      const color = parse(colorStr);
      if (color) {
        const rgbColor = rgb(color);
        if (rgbColor && rgbColor.mode === "rgb") {
          r = Math.round((rgbColor.r ?? 0) * 255);
          g = Math.round((rgbColor.g ?? 0) * 255);
          b = Math.round((rgbColor.b ?? 0) * 255);
        }
      }
    } catch (e) {
      return {
        isVibrant: false,
        description: "unknown",
        saturation: "0.00",
        brightness: "0.00",
      };
    }

    // Calculate saturation and brightness
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const saturation = max === 0 ? 0 : (max - min) / max;
    const brightness = max / 255;

    // Vibrant = high saturation (>0.3) and decent brightness (>0.2)
    const isVibrant = saturation > 0.3 && brightness > 0.2;

    // Describe the color
    let description = "";
    if (g > r && g > b && g > 100) description = "green";
    else if (b > r && b > g && b > 100) description = "blue";
    else if (r > g && r > b && r > 100) description = "red/orange";
    else if (max < 50) description = "dark";
    else if (min > 200) description = "light/white";
    else description = "neutral";

    return {
      isVibrant,
      description,
      saturation: saturation.toFixed(2),
      brightness: brightness.toFixed(2),
    };
  };

  // Collect class patterns for framework detection
  const allClasses = new Set<string>();
  if (buttons && buttons.length > 0) {
    buttons.forEach(btn => {
      if (btn.classes) {
        btn.classes.split(/\s+/).forEach(cls => {
          if (cls.length > 0 && cls.length < 50) {
            allClasses.add(cls);
          }
        });
      }
    });
  }

  // Add framework detection hints
  if (allClasses.size > 0) {
    const classSample = Array.from(allClasses).slice(0, 50).join(", ");
    prompt += `\n## CSS Class Patterns (for framework detection):\n`;
    prompt += `Sample classes: ${classSample}\n`;

    // Add framework hints from meta/scripts
    if (
      (jsAnalysis as any).__framework_hints &&
      (jsAnalysis as any).__framework_hints.length > 0
    ) {
      prompt += `Framework hints from page: ${(jsAnalysis as any).__framework_hints.join(", ")}\n`;
    }

    prompt += `\n**Framework Detection Patterns:**\n`;
    prompt += `- Tailwind: Look for utility classes like \`flex\`, \`items-center\`, \`px-*\`, \`py-*\`, \`bg-*-500\`, \`rounded-*\`, \`text-*\`, \`space-x-*\`, \`gap-*\`\n`;
    prompt += `- Bootstrap: Look for \`btn\`, \`btn-primary\`, \`container\`, \`row\`, \`col-*\`, \`d-flex\`, \`justify-*\`, \`mb-*\`, \`mt-*\`\n`;
    prompt += `- Material UI: Look for \`MuiButton\`, \`Mui*\`, \`makeStyles\`, or modern Material classes\n`;
    prompt += `- Chakra UI: Look for \`chakra-*\`, minimal utility-style classes, or data attributes\n`;
    prompt += `- Custom: Mixed or unique class patterns that don't match standard frameworks\n\n`;
  }

  // Add button context with detailed info
  if (buttons && buttons.length > 0) {
    prompt += `## Detected Buttons (${buttons.length} total):\n`;

    // First, show a color summary to help LLM see distinct options
    const colorSummary = new Map<string, number[]>();
    buttons.forEach((btn, idx) => {
      const bg = btn.background || "transparent";
      if (!colorSummary.has(bg)) {
        colorSummary.set(bg, []);
      }
      colorSummary.get(bg)!.push(idx);
    });

    prompt += `\n**COLOR GROUPS** (buttons sharing the same background color):\n`;
    colorSummary.forEach((indices, color) => {
      const count = indices.length;
      const colorInfo = getColorInfo(color);
      prompt += `- ${color} (${colorInfo.description}${colorInfo.isVibrant ? " - VIBRANT" : ""}) → Buttons ${indices.join(", ")} (${count} button${count > 1 ? "s" : ""})\n`;
    });
    prompt += `\n⚠️ **CRITICAL**: Primary and secondary MUST be from DIFFERENT color groups!\n\n`;

    prompt += `Analyze these buttons and identify which is the PRIMARY CTA and which is SECONDARY:\n\n`;

    buttons.forEach((btn, idx) => {
      const bgInfo = getColorInfo(btn.background);

      prompt += `**Button #${idx}:**\n`;
      prompt += `- Text: "${btn.text}"\n`;
      prompt += `- Background Color: ${btn.background} (${bgInfo.description}${bgInfo.isVibrant ? " - VIBRANT/BRAND COLOR" : ""})\n`;
      prompt += `- Text Color: ${btn.textColor}\n`;
      if (btn.borderColor) prompt += `- Border Color: ${btn.borderColor}\n`;
      if (btn.borderRadius) prompt += `- Border Radius: ${btn.borderRadius}\n`;
      prompt += `- Classes: ${btn.classes.substring(0, 150)}${btn.classes.length > 150 ? "..." : ""}\n`;
      prompt += `\n`;
    });
  }

  // Add logo candidates section (optimized - compact format)
  if (logoCandidates && logoCandidates.length > 0) {
    prompt += `\n## Logo Candidates (${logoCandidates.length}):\n`;
    if (input.screenshot) {
      prompt += `**IMPORTANT**: Look at the screenshot provided. The brand logo is almost always in the TOP/HEADER area of the page.\n`;
      prompt += `Find the logo in the header area of the screenshot, then match it to one of the candidates below.\n\n`;
    } else {
      prompt += `**IMPORTANT**: The brand logo is almost always in the TOP/HEADER area of the page.\n`;
      prompt += `Find the logo in the header area (usually the top of the page), then match it to one of the candidates below.\n\n`;
    }

    if (brandName) {
      prompt += `Brand Name: "${brandName}" - The logo should visually represent or contain this name.\n\n`;
    }

    // Compact format: index, location, visible, alt, indicators, href, truncated URL
    logoCandidates.forEach((candidate, idx) => {
      const indicators: string[] = [];
      if (candidate.indicators.inHeader) indicators.push("header");
      if (candidate.indicators.altMatch) indicators.push("alt=logo");
      if (candidate.indicators.srcMatch) indicators.push("url=logo");
      if (candidate.indicators.classMatch) indicators.push("class=logo");
      if (candidate.indicators.hrefMatch) indicators.push("href=home");

      const urlPreview =
        candidate.src.length > 80
          ? candidate.src.substring(0, 80) + "..."
          : candidate.src;

      const hrefInfo = candidate.href ? ` | href:${candidate.href}` : "";
      const typeLabel = candidate.isSvg ? "SVG" : "IMG";

      prompt += `#${idx}: ${candidate.location} | ${candidate.isVisible ? "visible" : "hidden"} | ${typeLabel} | alt:"${candidate.alt || ""}" | [${indicators.join(", ")}]${hrefInfo} | ${urlPreview}\n`;
    });

    prompt += `\n**LOGO SELECTION - SIMPLE APPROACH:**\n`;
    if (input.screenshot) {
      prompt += `Look at the screenshot and select the MOST PROMINENT primary brand logo.\n\n`;
    } else {
      prompt += `Select the MOST PROMINENT primary brand logo.\n\n`;
    }
    prompt += `**Simple Rules:**\n`;
    prompt += `1. **Look at the TOP of the page** - The main logo is almost always in the header/navbar at the very top\n`;
    prompt += `2. **Primary logo** - Choose the largest, most visible logo that represents "${brandName || "the website's brand"}"\n`;
    prompt += `3. **Prefer header logos** - Logos in the header/navbar area are the brand logo (highest priority)\n`;
    prompt += `4. **Ignore partner/client logos** - Skip smaller logos in "customers", "partners", or footer sections\n`;
    if (input.screenshot) {
      prompt += `5. **Use the screenshot** - Visually identify which logo is THE main brand logo users see first\n\n`;
    } else {
      prompt += `5. **Use visual indicators** - Identify which logo is THE main brand logo based on position, size, and indicators\n\n`;
    }
    prompt += `**What to avoid:**\n`;
    prompt += `- Customer/client logos (usually smaller, in groups, different brand names)\n`;
    prompt += `- Social media icons\n`;
    prompt += `- Footer logos (unless no header logo exists)\n\n`;
    prompt += `Just pick the obvious main brand logo at the top of the page that users see first.\n\n`;
  }

  // Add background color candidates section
  if (backgroundCandidates && backgroundCandidates.length > 0) {
    prompt += `\n## Background Color Candidates (${backgroundCandidates.length}):\n`;
    if (input.screenshot) {
      prompt += `Multiple background colors were detected. Use the screenshot to identify which is the actual page background:\n\n`;
    } else {
      prompt += `Multiple background colors were detected. Identify which is the actual page background:\n\n`;
    }

    backgroundCandidates.forEach((candidate, idx) => {
      const areaInfo = candidate.area
        ? ` | area: ${Math.round(candidate.area)}px²`
        : "";
      prompt += `#${idx}: ${candidate.color} | source: ${candidate.source} | priority: ${candidate.priority}${areaInfo}\n`;
    });

    prompt += `\n**Selection Rules:** `;
    if (input.screenshot) {
      prompt += `Use the screenshot to visually identify the main page background. Consider:\n`;
      prompt += `- Color scheme (dark mode should have dark background, light mode should have light background)\n`;
      prompt += `- Most visible/largest area in the screenshot\n`;
    } else {
      prompt += `Identify the main page background based on priority and source. Consider:\n`;
      prompt += `- Color scheme (dark mode should have dark background, light mode should have light background)\n`;
      prompt += `- Highest priority sources (body/html > CSS vars > containers)\n`;
      prompt += `- Largest area coverage\n`;
    }
    prompt += `- Higher priority sources (body/html > CSS vars > containers)\n`;
    prompt += `- Return the hex color in the colorRoles.backgroundColor field\n\n`;
  }

  // Add specific questions
  prompt += `\n## Your Task:\n`;

  if (buttons && buttons.length > 0) {
    prompt += `1. **PRIMARY Button**: Identify which button (by index 0-${buttons.length - 1}) is the main call-to-action.\n`;
    prompt += `   - **CRITICAL**: Buttons with VIBRANT/BRAND COLOR backgrounds (like green, blue, orange) are ALMOST ALWAYS the primary CTA\n`;
    prompt += `   - **STRONG INDICATORS**: Look for these class patterns (very high priority):\n`;
    prompt += `     * \`bg-brand-400\`, \`bg-brand-500\`, or similar brand utility classes\n`;
    prompt += `     * \`bg-green-*\`, \`bg-blue-*\`, \`bg-purple-*\` with high numbers (400+)\n`;
    prompt += `     * Any class containing "brand", "primary", or "cta"\n`;
    prompt += `   - Look for: Bright, saturated colors (green, blue, purple, orange) + action-oriented text\n`;
    prompt += `   - Action-oriented text examples: "Get Started", "Sign Up", "Start Free", "Start your Project", "Try Now", "Get Started Free"\n`;
    prompt += `   - If a button has BOTH vibrant color AND strong CTA text, it's DEFINITELY the primary\n`;
    prompt += `   - Avoid buttons with transparent, white, or muted gray backgrounds UNLESS no vibrant buttons exist\n`;
    prompt += `   - Return the button INDEX (not text) and explain your reasoning\n\n`;

    prompt += `2. **SECONDARY Button**: Identify which button is secondary (outline, ghost, or less prominent).\n`;
    prompt += `   - **CRITICAL**: MUST have a DIFFERENT background color than the primary button you selected\n`;
    prompt += `   - **EXAMPLES OF VALID COMBINATIONS**:\n`;
    prompt += `     * Primary: #00C853 (green, vibrant) → Secondary: transparent (outline style) ✓\n`;
    prompt += `     * Primary: #1976D2 (blue, vibrant) → Secondary: #FFFFFF (white/light) ✓\n`;
    prompt += `     * Primary: #FF6B35 (orange, vibrant) → Secondary: #F5F5F5 (gray/subtle) ✓\n`;
    prompt += `   - **INVALID COMBINATION**:\n`;
    prompt += `     * Primary: #00C853 → Secondary: #00C853 ✗ SAME COLOR - NOT ALLOWED!\n`;
    prompt += `   - Usually has transparent/subtle background, border, or muted colors\n`;
    prompt += `   - Common for actions like "Login", "Learn More", "Contact", "Documentation"\n`;
    prompt += `   - Often has an outline/border instead of filled background\n`;
    prompt += `   - Look at the COLOR GROUPS above - pick from a DIFFERENT group than primary\n`;
    prompt += `   - If all remaining buttons have the same color as primary, set secondaryButtonIndex to -1\n`;
    prompt += `   - Return the button INDEX and reasoning\n\n`;
  }

  prompt += `${buttons && buttons.length > 0 ? "3" : "1"}. **Color Roles**: Based on ${buttons && buttons.length > 0 ? "button colors and " : ""}page context:\n`;
  prompt += `   - PRIMARY brand color (usually logo/heading color)\n`;
  prompt += `   - ACCENT color (${buttons && buttons.length > 0 ? "usually the vibrant CTA button background - green, blue, etc." : "vibrant accent color from the page"})\n`;
  prompt += `   - Background and text colors\n\n`;

  prompt += `${buttons && buttons.length > 0 ? "4" : "2"}. **Brand Personality**: Overall tone and energy\n\n`;

  prompt += `${buttons && buttons.length > 0 ? "5" : "3"}. **Design System**: Based on the class patterns shown above:\n`;
  prompt += `   - **Framework**: Identify the CSS framework (tailwind/bootstrap/material/chakra/custom/unknown)\n`;
  prompt += `   - **Component Library**: Look for prefixes like \`radix-\`, \`shadcn-\`, \`headlessui-\`, or \`react-aria-\` in classes\n`;
  prompt += `   - If using Tailwind + a component library, identify both (e.g., framework: tailwind, componentLibrary: "radix-ui")\n\n`;

  prompt += `${buttons && buttons.length > 0 ? "6" : "4"}. **Clean Fonts**: Return up to 5 cleaned, human-readable font names\n`;
  prompt += `   - Remove framework obfuscation (Next.js hashes, etc.)\n`;
  prompt += `   - Filter out generics and CSS variables\n`;
  prompt += `   - Prioritize by frequency (shown in usage count)\n`;
  prompt += `   - Assign appropriate roles (heading, body, monospace, display)\n\n`;

  if (logoCandidates && logoCandidates.length > 0) {
    const logoTaskNumber = buttons && buttons.length > 0 ? "7" : "5";
    prompt += `${logoTaskNumber}. **Logo Selection**: Identify the best brand logo from the ${logoCandidates.length} candidates provided above.\n`;
    prompt += `   - **YOU MUST RETURN**: selectedLogoIndex (number), selectedLogoReasoning (string), and confidence (0-1)\n`;
    prompt += `   - **CRITICAL**: The logo MUST match the brand name "${brandName || "unknown"}" and look like a brand logo\n`;
    prompt += `   - **IT'S OK TO RETURN -1**: If no candidate is a good brand logo, return -1 with low confidence\n`;
    prompt += `   - **DECISION PROCESS**:\n`;
    if (input.screenshot) {
      prompt += `     1. Look at the screenshot - find the logo in the HEADER/TOP area\n`;
      prompt += `     2. Check which candidate matches that visual position and appearance\n`;
    } else {
      prompt += `     1. Find the logo in the HEADER/TOP area based on candidate indicators\n`;
      prompt += `     2. Check which candidate matches the expected position and appearance\n`;
    }
    prompt += `     3. Verify the candidate has indicators: "header", "href=home", or "alt=logo"\n`;
    prompt += `     4. Verify it's NOT a UI icon (search, menu, cart, user, settings, etc.)\n`;
    prompt += `     5. If multiple candidates look similar, prefer the one with href="/" (homepage link)\n`;
    prompt += `     6. If you're unsure or none look like brand logos, return -1\n`;
    prompt += `   - **STRONG INDICATORS** (prioritize candidates with these):\n`;
    prompt += `     * "href:/" or "hrefMatch" → Logo links to homepage (VERY STRONG indicator of brand logo)\n`;
    prompt += `     * Internal links only → Logo should NOT link to external websites\n`;
    prompt += `     * Has a link → Brand logos are usually clickable, not inside buttons\n`;
    prompt += `     * "header" location → Logo in header/navbar area (STRONG indicator)\n`;
    prompt += `     * "visible" → Logo is currently visible (preferred)\n`;
    prompt += `     * "alt=logo" or alt text matching "${brandName || "brand name"}" → Likely the brand logo\n`;
    prompt += `     * Reasonable size (not tiny like icons, not huge like hero images)\n`;
    prompt += `   - **AVOID** (return -1 if ALL candidates are these):\n`;
    prompt += `     * Very large images (likely og:image, hero images, or banners - not logos)\n`;
    prompt += `     * Images with NO LINK or inside buttons (brand logos link to homepage via <a> tag)\n`;
    prompt += `     * Images that link to EXTERNAL websites (brand logos link to homepage, not external sites)\n`;
    prompt += `     * UI icons: search icons, menu hamburgers, cart icons, user icons, settings icons\n`;
    prompt += `     * Very small square icons (< 40x40px)\n`;
    prompt += `     * Language switcher flags or text\n`;
    prompt += `     * Customer/client logos (different brand names than "${brandName || "unknown"}")\n`;
    prompt += `     * Partner/testimonial logos (usually in body, not header)\n`;
    prompt += `     * Footer logos (unless no header logo exists)\n`;
    prompt += `     * GitHub stars, social media icons, badges, external service badges\n`;
    prompt += `     * Logos in "customers", "partners", "case studies" sections\n`;
    prompt += `   - **RETURN FORMAT**:\n`;
    prompt += `     * selectedLogoIndex: The INDEX number (0-${logoCandidates.length - 1}) of the best logo, or -1 if none are good brand logos\n`;
    prompt += `     * selectedLogoReasoning: "Selected #X because [it has href:/, in header, matches brand name, etc.]" OR "No valid brand logo found - candidates are UI icons/customer logos"\n`;
    prompt += `     * confidence: 0.8-1.0 if sure, 0.5-0.7 if uncertain, 0.0-0.4 if no good match or returning -1\n`;
    prompt += `   - **PREFER -1 OVER BAD LOGOS**: Better to return no logo than a search icon or customer logo\n\n`;
  }

  if (buttons && buttons.length > 0) {
    prompt += `## VALIDATION CHECKLIST - VERIFY BEFORE RESPONDING:\n`;
    prompt += `Before finalizing your answer, check:\n`;
    prompt += `1. ✓ Are primaryButtonIndex and secondaryButtonIndex DIFFERENT numbers?\n`;
    prompt += `2. ✓ Do they have DIFFERENT background colors? (Compare the actual hex/color values)\n`;
    prompt += `3. ✓ If you selected buttons from the same COLOR GROUP, go back and pick a different secondary\n`;
    prompt += `4. ✓ If no valid secondary exists (all buttons same color as primary), set secondaryButtonIndex to -1\n\n`;

    prompt += `## FINAL RULES:\n`;
    prompt += `- Primary and secondary buttons MUST have different background colors (not just different shades - completely different colors)\n`;
    prompt += `- Primary and secondary buttons MUST be different buttons (different indices)\n`;
    prompt += `- Refer to the COLOR GROUPS section above to ensure you're picking from different groups\n`;
    prompt += `- Be decisive and confident. Prioritize vibrant, saturated colors over neutral ones for primary buttons\n`;
    prompt += `- If no clear primary/secondary exists with different colors, return -1 for that index\n`;
  }

  // CRITICAL: Ensure LLM returns all required fields
  prompt += `\n## ⚠️ CRITICAL: YOU MUST RETURN ALL REQUIRED FIELDS\n`;
  prompt += `The response schema REQUIRES these fields. DO NOT return empty objects {}.\n`;
  prompt += `\n**REQUIRED FIELDS:**\n`;
  let fieldNumber = 1;
  if (buttons && buttons.length > 0) {
    prompt += `${fieldNumber}. buttonClassification: { primaryButtonIndex, primaryButtonReasoning, secondaryButtonIndex, secondaryButtonReasoning, confidence }\n`;
    fieldNumber++;
  }
  prompt += `${fieldNumber}. colorRoles: { primaryColor, accentColor, backgroundColor, textPrimary, confidence }\n`;
  fieldNumber++;
  prompt += `${fieldNumber}. cleanedFonts: [] (array, can be empty but must be present)\n`;
  if (logoCandidates && logoCandidates.length > 0) {
    fieldNumber++;
    prompt += `${fieldNumber}. logoSelection: { selectedLogoIndex, selectedLogoReasoning, confidence }\n`;
  }
  prompt += `\n**DO NOT** return empty objects {}. Fill in ALL fields with actual values or -1/null as appropriate.\n`;

  return prompt;
}
