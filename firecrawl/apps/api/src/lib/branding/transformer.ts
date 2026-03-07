import { processRawBranding } from "./processor";
import { config } from "../../config";
import { BrandingProfile } from "../../types/branding";
import { enhanceBrandingWithLLM } from "./llm";
import { Meta } from "../../scraper/scrapeURL";
import { Document } from "../../controllers/v2/types";
import { BrandingScriptReturn, ButtonSnapshot } from "./types";
import { mergeBrandingResults } from "./merge";
import {
  selectLogoWithConfidence,
  shouldUseLLMForLogoSelection,
  getTopCandidatesForLLM,
} from "./logo-selector";
import { calculateLogoArea } from "./types";

export async function brandingTransformer(
  meta: Meta,
  document: Document,
  rawBranding: BrandingScriptReturn,
): Promise<BrandingProfile> {
  let jsBranding = processRawBranding(rawBranding);

  if (!jsBranding) {
    return {};
  }

  let brandingProfile: BrandingProfile = jsBranding;

  try {
    const buttonSnapshots: ButtonSnapshot[] =
      (jsBranding as any).__button_snapshots || [];
    const inputSnapshots = (jsBranding as any).__input_snapshots || [];

    const logoCandidates = rawBranding.logoCandidates || [];
    const brandName = rawBranding.brandName;
    const backgroundCandidates = rawBranding.backgroundCandidates || [];

    // TIER 1: Use smart heuristics to get initial selection
    const heuristicResult =
      logoCandidates.length > 0
        ? selectLogoWithConfidence(logoCandidates, brandName)
        : null;

    if (logoCandidates.length === 0) {
      meta.logger.warn("No logo candidates found", { brandName });
    } else {
      meta.logger.info("Logo heuristic selection", {
        candidatesCount: logoCandidates.length,
        selectedIndex: heuristicResult?.selectedIndex,
        confidence: heuristicResult?.confidence,
        method: heuristicResult?.method,
      });
    }

    // TIER 2: Decide if we need LLM validation
    const needsLLMValidation = heuristicResult
      ? shouldUseLLMForLogoSelection(heuristicResult.confidence)
      : false;

    // Filter to top 10 candidates for LLM (reduces token cost)
    const { filteredCandidates, indexMap } =
      needsLLMValidation && logoCandidates.length > 0
        ? getTopCandidatesForLLM(logoCandidates, 10)
        : { filteredCandidates: [], indexMap: new Map<number, number>() };

    // Limit buttons to top 12 most relevant ones to reduce prompt size
    // Prioritize buttons with CTA indicators, vibrant colors, or common CTA text
    let limitedButtons = buttonSnapshots;
    const buttonIndexMap = new Map<number, number>(); // LLM index -> original index

    if (buttonSnapshots.length > 12) {
      const scored = buttonSnapshots
        .map((btn, idx) => {
          let score = 0;
          const text = (btn.text || "").toLowerCase();
          const bgColor = btn.background || "";

          // Score common CTA text (higher priority)
          const primaryCtaKeywords = [
            "get started",
            "sign up",
            "sign in",
            "login",
            "register",
            "read",
            "learn",
            "download",
            "buy",
            "shop",
          ];
          if (primaryCtaKeywords.some(keyword => text.includes(keyword)))
            score += 100;

          // Score secondary CTA text
          const secondaryCtaKeywords = ["try", "start", "view", "explore"];
          if (secondaryCtaKeywords.some(keyword => text.includes(keyword)))
            score += 50;

          // Score vibrant colors (not white/transparent/gray)
          if (
            bgColor &&
            !bgColor.match(
              /transparent|white|#fff|#ffffff|gray|grey|#f[0-9a-f]{5}/i,
            )
          ) {
            score += 30;
          }

          return { btn, originalIdx: idx, score };
        })
        .sort((a, b) => b.score - a.score)
        .slice(0, 12);

      limitedButtons = scored.map(item => item.btn);

      // Create index map: LLM index (0-11) -> original index
      scored.forEach((item, llmIdx) => {
        buttonIndexMap.set(llmIdx, item.originalIdx);
      });
    } else {
      // No filtering needed, create identity map
      buttonSnapshots.forEach((_, idx) => {
        buttonIndexMap.set(idx, idx);
      });
    }

    meta.logger.info("Button filtering for LLM", {
      totalButtons: buttonSnapshots.length,
      limitedButtons: limitedButtons.length,
    });

    // TIER 2: Only call LLM if heuristics are uncertain
    const llmEnhancement = await enhanceBrandingWithLLM({
      jsAnalysis: jsBranding,
      buttons: limitedButtons,
      logoCandidates:
        needsLLMValidation && filteredCandidates.length > 0
          ? filteredCandidates
          : undefined, // Don't send logo candidates if heuristics are confident
      brandName,
      backgroundCandidates:
        backgroundCandidates.length > 0 ? backgroundCandidates : undefined,
      screenshot: document.screenshot,
      url: document.url || meta.url,
      teamId: meta.internalOptions.teamId,
    });

    // Map LLM's filtered index back to original index for logos
    if (needsLLMValidation && llmEnhancement.logoSelection) {
      const llmFilteredIndex = llmEnhancement.logoSelection.selectedLogoIndex;
      const llmOriginalIndex = indexMap.get(llmFilteredIndex);

      if (llmOriginalIndex !== undefined) {
        // Update the selection with the original index
        llmEnhancement.logoSelection.selectedLogoIndex = llmOriginalIndex;
      } else {
        // LLM returned invalid index - fallback to heuristic
        meta.logger.warn(
          "LLM returned invalid logo index, falling back to heuristic",
          {
            llmFilteredIndex,
            indexMapSize: indexMap.size,
            validIndices: Array.from(indexMap.keys()),
            heuristicIndex: heuristicResult?.selectedIndex,
          },
        );
        // Use heuristic result if available, otherwise clear LLM selection
        if (heuristicResult) {
          llmEnhancement.logoSelection = {
            selectedLogoIndex: heuristicResult.selectedIndex,
            selectedLogoReasoning: `Heuristic fallback (LLM returned invalid index): ${heuristicResult.reasoning}`,
            confidence: Math.max(heuristicResult.confidence - 0.1, 0.3), // Slightly lower confidence
          };
        } else {
          // No heuristic result available - clear LLM selection
          llmEnhancement.logoSelection = undefined;
        }
      }
    }

    // Map LLM's button indices back to original indices
    if (llmEnhancement.buttonClassification) {
      const llmPrimaryIdx =
        llmEnhancement.buttonClassification.primaryButtonIndex;
      const llmSecondaryIdx =
        llmEnhancement.buttonClassification.secondaryButtonIndex;

      if (llmPrimaryIdx >= 0) {
        const originalPrimaryIdx = buttonIndexMap.get(llmPrimaryIdx);
        if (originalPrimaryIdx !== undefined) {
          llmEnhancement.buttonClassification.primaryButtonIndex =
            originalPrimaryIdx;
        }
      }

      if (llmSecondaryIdx >= 0) {
        const originalSecondaryIdx = buttonIndexMap.get(llmSecondaryIdx);
        if (originalSecondaryIdx !== undefined) {
          llmEnhancement.buttonClassification.secondaryButtonIndex =
            originalSecondaryIdx;
        }
      }
    }

    // TIER 3: Merge heuristic and LLM results
    if (heuristicResult && logoCandidates.length > 0) {
      if (needsLLMValidation && llmEnhancement.logoSelection) {
        // LLM validation was used - validate it against heuristic
        const llmOriginalIndex = llmEnhancement.logoSelection.selectedLogoIndex;
        const heuristicSelectedIndex = heuristicResult.selectedIndex;

        // If LLM picked a different logo, validate it's not worse
        if (
          llmOriginalIndex !== undefined &&
          llmOriginalIndex !== heuristicSelectedIndex &&
          llmOriginalIndex >= 0 &&
          llmOriginalIndex < logoCandidates.length
        ) {
          const llmCandidate = logoCandidates[llmOriginalIndex];
          const heuristicCandidate = logoCandidates[heuristicSelectedIndex];

          // Calculate logo sizes
          const llmArea = calculateLogoArea(llmCandidate.position);
          const heuristicArea = calculateLogoArea(heuristicCandidate.position);

          // Red flags: LLM picked a logo that's objectively worse
          const llmIsWorse =
            // LLM picked non-header logo when heuristic picked header logo
            (!llmCandidate.indicators?.inHeader &&
              heuristicCandidate.indicators?.inHeader) ||
            // LLM picked non-visible logo when heuristic picked visible logo
            (!llmCandidate.isVisible && heuristicCandidate.isVisible) ||
            // LLM picked logo without href="/" when heuristic has it
            (!llmCandidate.indicators?.hrefMatch &&
              heuristicCandidate.indicators?.hrefMatch) ||
            // LLM picked logo from body/footer when heuristic picked from header
            (llmCandidate.location !== "header" &&
              heuristicCandidate.location === "header") ||
            // LLM picked much smaller logo (less than 50% of heuristic area) AND heuristic is in header
            (llmArea < heuristicArea * 0.5 &&
              heuristicCandidate.indicators?.inHeader) ||
            // LLM picked very small logo (<500px²) when heuristic picked reasonable size
            (llmArea < 500 && heuristicArea > 1000);

          if (llmIsWorse) {
            meta.logger.warn(
              "LLM picked objectively worse logo - using heuristic instead",
              {
                llmOriginalIndex: llmOriginalIndex,
                llmLocation: llmCandidate.location,
                llmVisible: llmCandidate.isVisible,
                llmInHeader: llmCandidate.indicators?.inHeader,
                llmHrefMatch: llmCandidate.indicators?.hrefMatch,
                llmArea: Math.round(llmArea),
                llmSrc: llmCandidate.src.substring(0, 100),
                heuristicIndex: heuristicSelectedIndex,
                heuristicLocation: heuristicCandidate.location,
                heuristicVisible: heuristicCandidate.isVisible,
                heuristicInHeader: heuristicCandidate.indicators?.inHeader,
                heuristicHrefMatch: heuristicCandidate.indicators?.hrefMatch,
                heuristicArea: Math.round(heuristicArea),
                heuristicSrc: heuristicCandidate.src.substring(0, 100),
              },
            );

            // Override LLM with heuristic
            llmEnhancement.logoSelection = {
              selectedLogoIndex: heuristicResult.selectedIndex,
              selectedLogoReasoning: `Heuristic preferred over LLM (LLM picked worse logo): ${heuristicResult.reasoning}`,
              confidence: heuristicResult.confidence,
            };
          } else {
            // LLM picked something different but not objectively worse - trust it
            meta.logger.info(
              "Using LLM-validated logo selection (different from heuristic but valid)",
            );
          }
        } else {
          // LLM agreed with heuristic or picked invalid index
          meta.logger.info("Using LLM-validated logo selection");
        }
      } else if (!needsLLMValidation) {
        // Heuristics were confident - use them directly
        meta.logger.info(
          "Using heuristic logo selection (high confidence, skipped LLM)",
        );
        llmEnhancement.logoSelection = {
          selectedLogoIndex: heuristicResult.selectedIndex,
          selectedLogoReasoning: heuristicResult.reasoning,
          confidence: heuristicResult.confidence,
        };
      } else if (!llmEnhancement.logoSelection) {
        // LLM was called but didn't return selection - fallback to heuristic
        meta.logger.warn(
          "LLM validation requested but didn't return selection, using heuristic",
        );
        llmEnhancement.logoSelection = {
          selectedLogoIndex: heuristicResult.selectedIndex,
          selectedLogoReasoning: `Heuristic fallback: ${heuristicResult.reasoning}`,
          confidence: Math.max(heuristicResult.confidence - 0.1, 0.3), // Slightly lower confidence
        };
      }
    }

    meta.logger.info("Branding enhancement complete", {
      primary_btn_index: llmEnhancement.buttonClassification.primaryButtonIndex,
      secondary_btn_index:
        llmEnhancement.buttonClassification.secondaryButtonIndex,
      button_confidence: llmEnhancement.buttonClassification.confidence,
      color_confidence: llmEnhancement.colorRoles.confidence,
      logo_selected_index: llmEnhancement.logoSelection?.selectedLogoIndex,
      logo_confidence: llmEnhancement.logoSelection?.confidence,
      logo_selection_method: needsLLMValidation
        ? llmEnhancement.logoSelection
          ? "llm-validated"
          : "heuristic-fallback"
        : "heuristic-only",
      llm_called_for_logo: needsLLMValidation,
    });

    brandingProfile = mergeBrandingResults(
      jsBranding,
      llmEnhancement,
      buttonSnapshots,
      logoCandidates.length > 0 ? logoCandidates : undefined,
    );

    meta.logger.info("Input fields detected", {
      count: inputSnapshots.length,
      types: inputSnapshots.map((i: any) => i.type).slice(0, 10),
    });
  } catch (error) {
    meta.logger.error(
      "LLM branding enhancement failed, using JS analysis only",
      { error },
    );
    brandingProfile = jsBranding;
  }

  if (config.DEBUG_BRANDING !== true) {
    delete (brandingProfile as any).__button_snapshots;
    delete (brandingProfile as any).__input_snapshots;
  }

  return brandingProfile;
}
