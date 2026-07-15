import type { ModelDescriptor } from "../api/contracts.ts";
import deepseekIconSource from "../../../public/assets/logos/deepseek.svg";
import doubaoIconSource from "../../../public/assets/logos/doubao.svg";
import geminiIconSource from "../../../public/assets/logos/gemini.svg";
import openAiIconSource from "../../../public/assets/logos/openai.svg";

const PROVIDER_ICON_SOURCE = {
  openai: openAiIconSource,
  deepseek: deepseekIconSource,
  gemini: geminiIconSource,
  doubao: doubaoIconSource,
} as const;

function providerName(model: ModelDescriptor): "openai" | "deepseek" | "gemini" | "doubao" {
  const identity = [model.model_id, model.display_name, ...model.aliases]
    .join(" ")
    .toLocaleLowerCase("en-US");
  if (identity.includes("deepseek")) return "deepseek";
  if (identity.includes("gemini")) return "gemini";
  if (identity.includes("doubao") || identity.includes("豆包")) return "doubao";
  return "openai";
}

export function ModelProviderIcon({ model }: { model: ModelDescriptor }) {
  const provider = providerName(model);
  return (
    <span className={`ex-model-provider-icon is-${provider}`} aria-hidden="true">
      <img src={PROVIDER_ICON_SOURCE[provider]} alt="" />
    </span>
  );
}
