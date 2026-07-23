import { request } from './request';
import type {
  ProviderItem,
  CreateProviderRequest,
  UpdateProviderRequest,
  ProviderTestResult,
  DefaultProvider,
  AsrConfig,
  TtsConfig,
  AsrConfigRequest,
  TtsConfigRequest,
} from '../types/llmProvider';

interface BackendProvider {
  id: string;
  base_url: string;
  model: string;
  embedding_model: string | null;
  embedding_dimensions: number | null;
  supports_embedding: boolean;
  temperature: number | null;
  masked_api_key: string;
  default_chat_provider: boolean;
  default_embedding_provider: boolean;
  is_enabled: boolean;
}

interface BackendGlobalSetting {
  default_chat_provider_id: string | null;
  default_embedding_provider_id: string | null;
  embedding_dimensions: number;
}

interface BackendTestResult {
  success: boolean;
  message: string | null;
  model: string | null;
  latency_ms?: number | null;
}

interface BackendVoiceTestResult {
  success: boolean;
  message: string | null;
  model: string | null;
}

interface BackendAsrConfig {
  url: string;
  model: string;
  masked_api_key: string;
  language: string;
  format: string;
  sample_rate: number;
  enable_turn_detection: boolean;
  turn_detection_type: string;
  turn_detection_threshold: number;
  turn_detection_silence_duration_ms: number;
}

interface BackendTtsConfig {
  model: string;
  masked_api_key: string;
  voice: string;
  format: string;
  sample_rate: number;
  mode: string;
  language_type: string;
  speech_rate: number;
  volume: number;
}

function fromBackendProvider(p: BackendProvider): ProviderItem {
  return {
    id: p.id,
    baseUrl: p.base_url,
    maskedApiKey: p.masked_api_key || '********',
    model: p.model,
    embeddingModel: p.embedding_model,
    embeddingDimensions: p.embedding_dimensions,
    supportsEmbedding: p.supports_embedding,
    temperature: p.temperature,
    defaultChatProvider: p.default_chat_provider,
    defaultEmbeddingProvider: p.default_embedding_provider,
    enabled: p.is_enabled,
  };
}

function toBackendProvider(req: CreateProviderRequest): Record<string, unknown> {
  return {
    id: req.id,
    base_url: req.baseUrl,
    api_key: req.apiKey,
    model: req.model,
    embedding_model: req.embeddingModel,
    embedding_dimensions: req.embeddingDimensions,
    supports_embedding: req.supportsEmbedding,
    temperature: req.temperature,
    is_enabled: req.enabled ?? true,
  };
}

function toBackendUpdateProvider(req: UpdateProviderRequest): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  if (req.baseUrl !== undefined) result.base_url = req.baseUrl;
  if (req.apiKey !== undefined) result.api_key = req.apiKey;
  if (req.model !== undefined) result.model = req.model;
  if (req.embeddingModel !== undefined) result.embedding_model = req.embeddingModel;
  if (req.embeddingDimensions !== undefined) result.embedding_dimensions = req.embeddingDimensions;
  if (req.supportsEmbedding !== undefined) result.supports_embedding = req.supportsEmbedding;
  if (req.temperature !== undefined) result.temperature = req.temperature;
  if (req.enabled !== undefined) result.is_enabled = req.enabled;
  return result;
}

function toProviderTestResult(result: BackendTestResult): ProviderTestResult {
  return {
    success: result.success,
    message: result.message || (result.success ? '测试成功' : '测试失败'),
    model: result.model || '',
  };
}

function toAsrConfig(res: BackendAsrConfig): AsrConfig {
  return {
    url: res.url,
    model: res.model,
    maskedApiKey: res.masked_api_key,
    language: res.language,
    format: res.format,
    sampleRate: res.sample_rate,
    enableTurnDetection: res.enable_turn_detection,
    turnDetectionType: res.turn_detection_type,
    turnDetectionThreshold: res.turn_detection_threshold,
    turnDetectionSilenceDurationMs: res.turn_detection_silence_duration_ms,
  };
}

function toTtsConfig(res: BackendTtsConfig): TtsConfig {
  return {
    model: res.model,
    maskedApiKey: res.masked_api_key,
    voice: res.voice,
    format: res.format,
    sampleRate: res.sample_rate,
    mode: res.mode,
    languageType: res.language_type,
    speechRate: res.speech_rate,
    volume: res.volume,
  };
}

function toBackendAsrRequest(data: AsrConfigRequest): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  if (data.url !== undefined) result.url = data.url;
  if (data.model !== undefined) result.model = data.model;
  if (data.apiKey !== undefined) result.api_key = data.apiKey;
  if (data.language !== undefined) result.language = data.language;
  if (data.format !== undefined) result.format = data.format;
  if (data.sampleRate !== undefined) result.sample_rate = data.sampleRate;
  if (data.enableTurnDetection !== undefined) result.enable_turn_detection = data.enableTurnDetection;
  if (data.turnDetectionType !== undefined) result.turn_detection_type = data.turnDetectionType;
  if (data.turnDetectionThreshold !== undefined) result.turn_detection_threshold = data.turnDetectionThreshold;
  if (data.turnDetectionSilenceDurationMs !== undefined) result.turn_detection_silence_duration_ms = data.turnDetectionSilenceDurationMs;
  return result;
}

function toBackendTtsRequest(data: TtsConfigRequest): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  if (data.model !== undefined) result.model = data.model;
  if (data.apiKey !== undefined) result.api_key = data.apiKey;
  if (data.voice !== undefined) result.voice = data.voice;
  if (data.format !== undefined) result.format = data.format;
  if (data.sampleRate !== undefined) result.sample_rate = data.sampleRate;
  if (data.mode !== undefined) result.mode = data.mode;
  if (data.languageType !== undefined) result.language_type = data.languageType;
  if (data.speechRate !== undefined) result.speech_rate = data.speechRate;
  if (data.volume !== undefined) result.volume = data.volume;
  return result;
}

export const llmProviderApi = {
  list: async (): Promise<ProviderItem[]> => {
    const res = await request.get<BackendProvider[]>('/api/admin/llm/providers');
    return res.map(fromBackendProvider);
  },

  get: async (_id: string): Promise<ProviderItem> => {
    const res = await request.get<BackendProvider>(`/api/admin/llm/providers/${_id}`);
    return fromBackendProvider(res);
  },

  create: async (data: CreateProviderRequest): Promise<{ id: string }> => {
    return request.post<{ id: string }>('/api/admin/llm/providers', toBackendProvider(data));
  },

  update: async (id: string, data: UpdateProviderRequest): Promise<void> => {
    return request.put<void>(`/api/admin/llm/providers/${id}`, toBackendUpdateProvider(data));
  },

  delete: async (id: string): Promise<void> => {
    return request.delete<void>(`/api/admin/llm/providers/${id}`);
  },

  test: async (id: string): Promise<ProviderTestResult> => {
    const res = await request.post<BackendTestResult>('/api/admin/llm/providers/test', { provider_id: id });
    return toProviderTestResult(res);
  },

  reload: (): Promise<void> => {
    return request.post<void>('/api/admin/llm/reload');
  },

  getDefaultProvider: async (): Promise<DefaultProvider> => {
    const res = await request.get<BackendGlobalSetting>('/api/admin/llm/settings');
    return {
      defaultProvider: res.default_chat_provider_id || '',
      defaultEmbeddingProvider: res.default_embedding_provider_id || '',
    };
  },

  updateDefaultProvider: async (data: DefaultProvider): Promise<void> => {
    await request.put<void>('/api/admin/llm/settings', {
      default_chat_provider_id: data.defaultProvider,
      default_embedding_provider_id: data.defaultEmbeddingProvider,
    });
  },

  updateDefaultEmbeddingProvider: async (data: DefaultProvider): Promise<void> => {
    await request.put<void>('/api/admin/llm/settings', {
      default_embedding_provider_id: data.defaultEmbeddingProvider,
    });
  },

  getAsrConfig: async (): Promise<AsrConfig> => {
    const res = await request.get<BackendAsrConfig>('/api/admin/llm/voice/asr');
    return toAsrConfig(res);
  },

  updateAsrConfig: async (data: AsrConfigRequest): Promise<void> => {
    await request.put<void>('/api/admin/llm/voice/asr', toBackendAsrRequest(data));
  },

  getTtsConfig: async (): Promise<TtsConfig> => {
    const res = await request.get<BackendTtsConfig>('/api/admin/llm/voice/tts');
    return toTtsConfig(res);
  },

  updateTtsConfig: async (data: TtsConfigRequest): Promise<void> => {
    await request.put<void>('/api/admin/llm/voice/tts', toBackendTtsRequest(data));
  },

  testAsr: async (): Promise<ProviderTestResult> => {
    const res = await request.post<BackendVoiceTestResult>('/api/admin/llm/voice/asr/test');
    return {
      success: res.success,
      message: res.message || (res.success ? '测试成功' : '测试失败'),
      model: res.model || '',
    };
  },
};
