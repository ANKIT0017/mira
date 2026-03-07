import { generateObject } from "ai";
import * as Sentry from "@sentry/node";
import { logger } from "../logger";
import { config } from "../../config";
import { BrandingEnhancement, brandingEnhancementSchema } from "./schema";
import { buildBrandingPrompt } from "./prompt";
import { BrandingLLMInput } from "./types";
import { getModel } from "../generic-ai";

export async function enhanceBrandingWithLLM(
  input: BrandingLLMInput,
): Promise<BrandingEnhancement> {
  const prompt = buildBrandingPrompt(input);

  // Smart model selection: use more powerful model for complex cases
  // gpt-4o-mini: cheaper, good for simple cases
  // gpt-4o: more capable, better for complex prompts with many buttons/logos
  const buttonsCount = input.buttons?.length || 0;
  const logoCandidatesCount = input.logoCandidates?.length || 0;
  const promptLength = prompt.length;

  // Use gpt-4o for complex cases:
  // - Many buttons (>8)
  // - Many logo candidates (>5)
  // - Long prompt (>8000 chars)
  // - Has screenshot (adds complexity)
  const isComplexCase =
    buttonsCount > 8 ||
    logoCandidatesCount > 5 ||
    promptLength > 8000 ||
    !!input.screenshot;

  const modelName = isComplexCase ? "gpt-4o" : "gpt-4o-mini";
  const model = getModel(modelName);

  if (config.DEBUG_BRANDING === true) {
    logger.info("LLM model selection", {
      model: modelName,
      buttonsCount,
      logoCandidatesCount,
      promptLength,
      hasScreenshot: !!input.screenshot,
      isComplexCase,
    });

    logger.debug("LLM branding prompt preview", {
      promptStart: prompt.substring(0, 500),
      promptEnd: prompt.substring(prompt.length - 500),
      buttonsPreview: input.buttons?.slice(0, 3).map(b => ({
        text: b.text?.substring(0, 50),
        background: b.background,
      })),
    });
  }

  try {
    const result = await generateObject({
      model,
      schema: brandingEnhancementSchema,
      providerOptions: {
        openai: {
          strictJsonSchema: true,
        },
      },
      messages: [
        {
          role: "system",
          content:
            "You are a brand design expert analyzing websites to extract accurate branding information.",
        },
        {
          role: "user",
          content: input.screenshot
            ? [
                { type: "text", text: prompt },
                { type: "image", image: input.screenshot },
              ]
            : prompt,
        },
      ],
      temperature: 0.1,
      experimental_telemetry: {
        isEnabled: true,
        functionId: "enhanceBrandingWithLLM",
        metadata: {
          teamId: input.teamId || "unknown",
        },
      },
    });

    if (config.DEBUG_BRANDING === true) {
      const reasoningPreview = result.reasoning
        ? result.reasoning.length > 1000
          ? result.reasoning.substring(0, 1000) + "..."
          : result.reasoning
        : undefined;

      logger.info("LLM branding response", {
        model: modelName,
        buttonsCount,
        logoCandidatesCount,
        promptLength,
        hasScreenshot: !!input.screenshot,
        usage: result.usage,
        finishReason: result.finishReason,
        reasoning: reasoningPreview,
        reasoningLength: result.reasoning?.length || 0,
        warnings: result.warnings,
        hasObject: !!result.object,
        objectKeys: result.object ? Object.keys(result.object) : [],
        buttonClassification: result.object?.buttonClassification,
        colorRoles: result.object?.colorRoles,
        cleanedFontsLength: result.object?.cleanedFonts?.length || 0,
        logoSelection: result.object?.logoSelection,
      });

      if (result.reasoning && result.reasoning.length > 1000) {
        logger.debug("LLM full reasoning", {
          reasoning: result.reasoning,
        });
      }
    }

    return result.object;
  } catch (error) {
    Sentry.captureException(error);

    logger.error("LLM branding enhancement failed", {
      error,
      buttonsCount: input.buttons?.length || 0,
      promptLength: prompt.length,
    });

    return {
      cleanedFonts: [],
      buttonClassification: {
        primaryButtonIndex: -1,
        primaryButtonReasoning: "LLM failed",
        secondaryButtonIndex: -1,
        secondaryButtonReasoning: "LLM failed",
        confidence: 0,
      },
      colorRoles: {
        confidence: 0,
      },
    };
  }
}
