import type { ConfigData } from "./hooks";

export const REDACTED_KEY = "********";

const PROVIDER_PREFIXES = new Set([
  "anthropic",
  "openai",
  "openrouter",
  "deepseek",
  "groq",
  "zhipu",
  "dashscope",
  "ollama",
  "custom",
]);

const NAMESPACED_MODEL_PROVIDERS = new Set(["openrouter"]);

export interface ProviderFormValues {
  provider: string;
  model: string;
  apiKey: string;
  apiBase: string;
  apiKeyDirty: boolean;
}

export interface ProviderSaveValues {
  provider: string;
  model: string;
  apiKey: string;
  apiBase: string;
  apiKeyDirty: boolean;
}

export interface WorkspaceSaveValues {
  workspace: string;
  agentName: string;
  maxTokens: number;
  temperature: number;
  restrict: boolean;
}

export function shouldInitializeProviderFromConfig(
  config: ConfigData | null | undefined,
  initialized: boolean,
): config is ConfigData {
  return Boolean(config) && !initialized;
}

export function parseProviderFromModel(model: string | undefined, fallback = "anthropic"): string {
  if (!model) return fallback;
  const [prefix] = model.split("/", 1);
  return prefix || fallback;
}

export function stripProviderPrefix(model: string, provider: string | undefined): string {
  if (!provider) return model;
  const prefix = `${provider}/`;
  let current = model;
  while (current.startsWith(prefix) && current.length > prefix.length) {
    current = current.slice(prefix.length);
  }
  return current;
}

function stripOtherKnownProviderPrefix(model: string, selectedProvider: string): string {
  const slashIndex = model.indexOf("/");
  if (slashIndex === -1) return model;
  const prefix = model.slice(0, slashIndex);
  if (prefix === selectedProvider || !PROVIDER_PREFIXES.has(prefix)) return model;
  return model.slice(slashIndex + 1);
}

export function modelForProviderSwitch(
  model: string,
  oldProvider: string,
  nextProvider: string,
): string {
  if (model.startsWith(`${nextProvider}/`)) return model;
  return stripProviderPrefix(model, oldProvider);
}

export function modelWithProviderPrefix(model: string, provider: string): string {
  if (!model) return model;
  let coreModel = stripProviderPrefix(model, provider);
  if (!NAMESPACED_MODEL_PROVIDERS.has(provider)) {
    coreModel = stripOtherKnownProviderPrefix(coreModel, provider);
  }
  return `${provider}/${coreModel}`;
}

export function providerCredentialsFromConfig(
  config: ConfigData | null | undefined,
  provider: string,
): Pick<ProviderFormValues, "apiKey" | "apiBase" | "apiKeyDirty"> {
  const providerConfig = config?.providers?.[provider];
  const currentApiKey = providerConfig?.apiKey ?? providerConfig?.api_key ?? "";
  return {
    apiKey: currentApiKey ? REDACTED_KEY : "",
    apiBase: providerConfig?.apiBase ?? providerConfig?.api_base ?? "",
    apiKeyDirty: false,
  };
}

export function initialProviderFormFromConfig(config: ConfigData): ProviderFormValues {
  const model = config.agents?.defaults?.model ?? "";
  const provider = parseProviderFromModel(model);
  return {
    provider,
    model,
    ...providerCredentialsFromConfig(config, provider),
  };
}

export function buildProviderConfigWriteUpdates(values: ProviderSaveValues): Record<string, unknown> {
  const providerUpdates: Record<string, unknown> = {};
  const updates: Record<string, unknown> = {
    agents: {
      defaults: {
        model: values.model
          ? modelWithProviderPrefix(values.model, values.provider)
          : values.model,
      },
    },
  };

  if (values.apiKeyDirty && values.apiKey !== REDACTED_KEY && values.apiKey !== "") {
    providerUpdates.api_key = values.apiKey;
  }
  if (values.apiBase) {
    providerUpdates.api_base = values.apiBase;
  }
  if (Object.keys(providerUpdates).length > 0) {
    updates.providers = { [values.provider]: providerUpdates };
  }

  return updates;
}

export function buildWorkspaceConfigWriteUpdates(values: WorkspaceSaveValues): Record<string, unknown> {
  return {
    agents: {
      defaults: {
        workspace: values.workspace,
        name: values.agentName,
        max_tokens: values.maxTokens,
        temperature: values.temperature,
      },
    },
    tools: {
      restrict_to_workspace: values.restrict,
    },
  };
}
