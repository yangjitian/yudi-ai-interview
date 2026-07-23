import { request } from './request';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

interface BackendSessionResponse {
  session_id: number;
  status: string;
  current_phase?: string | null;
  planned_duration?: number | null;
  web_socket_url?: string;
}

interface BackendSessionMeta {
  session_id: number;
  role_type: string;
  skill_id?: string | null;
  difficulty?: string | null;
  status?: string | null;
  current_phase?: string | null;
  planned_duration?: number | null;
  actual_duration?: number | null;
  start_time?: string | null;
  end_time?: string | null;
  evaluate_status?: string | null;
  evaluate_error?: string | null;
}

interface BackendEvaluationStatus {
  session_id: number;
  status?: string | null;
  evaluate_status: string | null;
  evaluate_error?: string | null;
  overall_score: number | null;
  overall_feedback?: string | null;
  strengths?: string[] | null;
  improvements?: string[] | null;
  reference_answers?: string[] | null;
  question_evaluations?: BackendQuestionEvaluation[] | null;
}

interface BackendQuestionEvaluation {
  question_index?: number | null;
  question?: string | null;
  category?: string | null;
  user_answer?: string | null;
  score?: number | null;
  feedback?: string | null;
  reference_answer?: string | null;
  key_points?: string[] | null;
}

interface BackendSessionListItem {
  sessionId: number;
  sessionIdStr: string;
  roleType: string;
  status: string;
  currentPhase: string;
  createdAt: string;
  updatedAt: string | null;
  actualDuration?: number;
  messageCount: number;
  evaluateStatus: string | null;
  evaluateError: string | null;
  overallScore?: number | null;
}

interface BackendMessage {
  id: number;
  session_id: number;
  message_type: string;
  phase: string;
  user_recognized_text: string;
  ai_generated_text: string;
  timestamp: string;
  sequence_num: number;
}

export interface CreateSessionRequest {
  roleType?: string;
  skillId: string;
  difficulty?: string;
  customJdText?: string;
  resumeId?: number;
  introEnabled?: boolean;
  techEnabled?: boolean;
  projectEnabled?: boolean;
  hrEnabled?: boolean;
  plannedDuration?: number;
  llmProvider?: string;
}

export interface SessionResponse {
  sessionId: string;
  roleType: string;
  currentPhase: string;
  status: string;
  startTime: string;
  plannedDuration: number;
  webSocketUrl: string;
}

export interface InterviewMessage {
  id: number;
  sessionId: number;
  messageType: string;
  phase: string;
  userRecognizedText: string;
  aiGeneratedText: string;
  timestamp: string;
  sequenceNum: number;
}

export interface VoiceAnswerDetail {
  questionIndex: number;
  question: string;
  category: string;
  userAnswer: string;
  score: number;
  feedback: string;
  referenceAnswer?: string | null;
  keyPoints?: string[] | null;
}

export interface VoiceEvaluationDetail {
  sessionId: string;
  totalQuestions: number;
  overallScore: number;
  overallFeedback: string;
  strengths: string[];
  improvements: string[];
  answers: VoiceAnswerDetail[];
}

export interface EvaluationStatusResponse {
  evaluateStatus: string | null;
  evaluateError?: string | null;
  evaluation?: VoiceEvaluationDetail | null;
}

export interface SessionMeta {
  sessionId: string;
  roleType: string;
  status: string;
  currentPhase: string;
  createdAt: string;
  updatedAt: string;
  actualDuration?: number;
  messageCount: number;
  evaluateStatus?: string;
  evaluateError?: string;
  overallScore?: number | null;
}

export interface WebSocketAudioMessage {
  type: 'audio';
  data: string;
  timestamp?: number;
}

export interface WebSocketSubtitleMessage {
  type: 'subtitle';
  text: string;
  isFinal: boolean;
}

export interface WebSocketAudioResponseMessage {
  type: 'audio';
  data: string;
  text: string;
}

export interface WebSocketTextMessage {
  type: 'text';
  content: string;
  final?: boolean;
}

export interface WebSocketAudioChunkMessage {
  type: 'audio_chunk';
  data: string;
  index: number;
  isLast: boolean;
}

export interface WebSocketControlResponseMessage {
  type: 'control';
  action: string;
  message?: string;
  timestamp?: number;
}

export interface WebSocketErrorMessage {
  type: 'error';
  message: string;
}

export type WebSocketMessage =
  | WebSocketAudioMessage
  | WebSocketSubtitleMessage
  | WebSocketAudioResponseMessage
  | WebSocketTextMessage
  | WebSocketAudioChunkMessage
  | WebSocketControlResponseMessage
  | WebSocketErrorMessage;

export interface WebSocketEventHandlers {
  onMessage?: (message: WebSocketMessage) => void;
  onSubtitle?: (text: string, isFinal: boolean) => void;
  onAudioResponse?: (audioData: string, text: string) => void;
  onTextResponse?: (text: string, isFinal: boolean) => void;
  onAudioChunk?: (data: string, index: number, isLast: boolean) => void;
  onControl?: (action: string, message?: string) => void;
  onErrorMessage?: (message: string) => void;
  onOpen?: () => void;
  onClose?: (event: CloseEvent) => void;
  onError?: (error: Event) => void;
}

function getWsBaseUrl(): string {
  const explicit = import.meta.env.VITE_WS_BASE_URL;
  if (explicit) return explicit;
  return API_BASE_URL.replace(/^http/i, 'ws');
}

function createWebSocketUrl(sessionId: string): string {
  return `${getWsBaseUrl().replace(/\/$/, '')}/api/voice/ws/${sessionId}`;
}

function clampQuestionCountFromDuration(plannedDuration?: number): number {
  const duration = plannedDuration || 25;
  const estimatedQuestionCount = Math.round(duration / 5);
  return Math.max(3, Math.min(20, estimatedQuestionCount));
}

function mapQuestionEvaluation(item: BackendQuestionEvaluation): VoiceAnswerDetail {
  return {
    questionIndex: item.question_index || 0,
    question: item.question || '',
    category: item.category || '',
    userAnswer: item.user_answer || '',
    score: item.score || 0,
    feedback: item.feedback || '',
    referenceAnswer: item.reference_answer || null,
    keyPoints: item.key_points || [],
  };
}

export const voiceInterviewApi = {
  async createSession(data: CreateSessionRequest): Promise<SessionResponse> {
    const questionCount = clampQuestionCountFromDuration(data.plannedDuration);
    const res = await request.post<BackendSessionResponse>('/api/voice/sessions', {
      skill_id: data.skillId,
      difficulty: data.difficulty || 'mid',
      question_count: questionCount,
      resume_id: data.resumeId,
      role_type: data.roleType || data.skillId,
      intro_enabled: data.introEnabled ?? true,
      tech_enabled: data.techEnabled ?? true,
      project_enabled: data.projectEnabled ?? true,
      hr_enabled: data.hrEnabled ?? true,
      planned_duration: data.plannedDuration || questionCount * 5,
      llm_provider: data.llmProvider,
      custom_jd_text: data.customJdText,
    });
    const wsBase = res.web_socket_url
      ? `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}${res.web_socket_url}`
      : createWebSocketUrl(String(res.session_id));
    return {
      sessionId: String(res.session_id),
      roleType: data.roleType || data.skillId,
      currentPhase: res.current_phase || 'INTRO',
      status: res.status,
      startTime: new Date().toISOString(),
      plannedDuration: res.planned_duration || data.plannedDuration || questionCount * 5,
      webSocketUrl: wsBase,
    };
  },

  async getSession(sessionId: string): Promise<SessionResponse> {
    const res = await request.get<BackendSessionMeta>(`/api/voice/sessions/${sessionId}`);
    return {
      sessionId: String(res.session_id),
      roleType: res.role_type,
      currentPhase: res.current_phase || '',
      status: res.status || 'CREATED',
      startTime: res.start_time || '',
      plannedDuration: res.planned_duration || 25,
      webSocketUrl: createWebSocketUrl(String(res.session_id)),
    };
  },

  async endSession(sessionId: string): Promise<void> {
    return request.post<void>(`/api/voice/sessions/${sessionId}/end`);
  },

  async getMessages(sessionId: string): Promise<InterviewMessage[]> {
    const res = await request.get<BackendMessage[]>(`/api/voice/sessions/${sessionId}/messages`);
    return res.map(m => ({
      id: m.id,
      sessionId: m.session_id,
      messageType: m.message_type,
      phase: m.phase,
      userRecognizedText: m.user_recognized_text,
      aiGeneratedText: m.ai_generated_text,
      timestamp: m.timestamp,
      sequenceNum: m.sequence_num,
    }));
  },

  async getEvaluation(sessionId: string): Promise<EvaluationStatusResponse> {
    const res = await request.get<BackendEvaluationStatus>(`/api/voice/sessions/${sessionId}/evaluation`);
    return {
      evaluateStatus: res.evaluate_status,
      evaluateError: res.evaluate_error,
      evaluation: res.overall_score !== null ? {
        sessionId: String(res.session_id),
        totalQuestions: (res.question_evaluations || []).length,
        overallScore: res.overall_score,
        overallFeedback: res.overall_feedback || '',
        strengths: res.strengths || [],
        improvements: res.improvements || [],
        answers: (res.question_evaluations || []).map(mapQuestionEvaluation),
      } : null,
    };
  },

  async generateEvaluation(sessionId: string): Promise<EvaluationStatusResponse> {
    await request.post(`/api/voice/sessions/${sessionId}/evaluation`);
    return this.getEvaluation(sessionId);
  },

  async pauseSession(sessionId: string): Promise<void> {
    return request.put(`/api/voice/sessions/${sessionId}/pause`);
  },

  async resumeSession(sessionId: string): Promise<SessionResponse> {
    const res = await request.put<BackendSessionResponse>(`/api/voice/sessions/${sessionId}/resume`);
    return {
      sessionId: String(res.session_id),
      roleType: '',
      currentPhase: res.current_phase || '',
      status: res.status,
      startTime: '',
      plannedDuration: res.planned_duration || 25,
      webSocketUrl: createWebSocketUrl(String(res.session_id)),
    };
  },

  async getAllSessions(): Promise<SessionMeta[]> {
    const res = await request.get<BackendSessionListItem[]>('/api/voice/sessions');
    return res.map(s => ({
      sessionId: String(s.sessionId),
      roleType: s.roleType,
      status: s.status,
      currentPhase: s.currentPhase,
      createdAt: s.createdAt,
      updatedAt: s.updatedAt || s.createdAt,
      actualDuration: s.actualDuration,
      messageCount: s.messageCount,
      evaluateStatus: s.evaluateStatus ?? undefined,
      evaluateError: s.evaluateError ?? undefined,
      overallScore: s.overallScore ?? null,
    }));
  },

  async deleteSession(sessionId: string): Promise<void> {
    return request.delete(`/api/voice-interview/sessions/${sessionId}`);
  },
};

export function subscribeVoiceEvaluationEvents(
  sessionId: string,
  onUpdate: () => void,
  onDisconnect: () => void,
): () => void {
  const eventSource = new EventSource(
    `${API_BASE_URL}/api/voice/sessions/${sessionId}/evaluation/events`,
  );
  eventSource.onmessage = onUpdate;
  eventSource.onerror = () => {
    eventSource.close();
    onDisconnect();
  };
  return () => eventSource.close();
}

export class VoiceInterviewWebSocket {
  private ws: WebSocket | null = null;
  private url: string;
  private handlers: WebSocketEventHandlers;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 3;
  private reconnectDelay = 2000;

  constructor(_sessionId: string, url: string, handlers: WebSocketEventHandlers) {
    this.url = url;
    this.handlers = handlers;
  }

  connect(): void {
    try {
      this.ws = new WebSocket(this.url);

      this.ws.onopen = () => {
        this.reconnectAttempts = 0;
        this.handlers.onOpen?.();
      };

      this.ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data) as WebSocketMessage;
          this.handlers.onMessage?.(message);

          switch (message.type) {
            case 'subtitle':
              this.handlers.onSubtitle?.(message.text, (message as WebSocketSubtitleMessage).isFinal);
              break;
            case 'audio':
              if ('text' in message) {
                const audioMsg = message as WebSocketAudioResponseMessage;
                this.handlers.onAudioResponse?.(audioMsg.data, audioMsg.text);
              }
              break;
            case 'audio_chunk':
              if ('index' in message) {
                const chunkMsg = message as WebSocketAudioChunkMessage;
                this.handlers.onAudioChunk?.(chunkMsg.data, chunkMsg.index, chunkMsg.isLast);
              }
              break;
            case 'text':
              if ('content' in message) {
                const textMsg = message as WebSocketTextMessage;
                this.handlers.onTextResponse?.(textMsg.content, Boolean(textMsg.final));
              }
              break;
            case 'control':
              {
                const controlMsg = message as WebSocketControlResponseMessage;
                this.handlers.onControl?.(controlMsg.action, controlMsg.message);
              }
              break;
            case 'error':
              this.handlers.onErrorMessage?.(message.message);
              break;
          }
        } catch {
          this.handlers.onErrorMessage?.('消息解析失败');
        }
      };

      this.ws.onclose = (event) => {
        this.handlers.onClose?.(event);
        if (!event.wasClean && this.reconnectAttempts < this.maxReconnectAttempts) {
          this.reconnectAttempts += 1;
          window.setTimeout(() => this.connect(), this.reconnectDelay);
        }
      };

      this.ws.onerror = (error) => {
        this.handlers.onError?.(error);
      };
    } catch {
      this.handlers.onErrorMessage?.('WebSocket 连接失败');
    }
  }

  disconnect(): void {
    this.ws?.close();
    this.ws = null;
  }

  send(data: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }

  sendAudio(data: string, timestamp?: number): void {
    this.send({ type: 'audio', data, timestamp });
  }

  sendControl(action: string, data?: Record<string, unknown>): void {
    this.send({ type: 'control', action, data, timestamp: Date.now() });
  }

  isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}

export function connectWebSocket(
  sessionId: string,
  webSocketUrl: string,
  handlers: WebSocketEventHandlers,
): VoiceInterviewWebSocket {
  const ws = new VoiceInterviewWebSocket(sessionId, webSocketUrl, handlers);
  ws.connect();
  return ws;
}

export default voiceInterviewApi;
