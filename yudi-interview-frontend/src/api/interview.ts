import { request } from './request';
import { historyApi } from './history';
import type {
  CreateInterviewRequest,
  CurrentQuestionResponse,
  InterviewReport,
  InterviewSession,
  SubmitAnswerRequest,
  SubmitAnswerResponse
} from '../types/interview';

const apiBaseURL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

export interface TextSessionMeta {
  sessionId: string;
  skillId: string;
  difficulty: string;
  resumeId: number | null;
  totalQuestions: number;
  status: string;
  evaluateStatus: string | null;
  evaluateError: string | null;
  overallScore: number | null;
  sourceType: string | null;
  knowledgeBaseId: number | null;
  interviewCategory?: string | null;
  createdAt: string;
  completedAt: string | null;
}

// Backend snake_case types
interface BackendSessionItem {
  id: number;
  session_id: string;
  skill_id: string;
  difficulty: string;
  resume_id: number | null;
  total_questions: number;
  overall_score: number | null;
  status: string;
  evaluate_status: string | null;
  evaluate_error: string | null;
  created_at: string;
  completed_at: string | null;
}

interface BackendSession {
  session_id: string;
  resume_text: string;
  total_questions: number;
  current_index: number;
  questions: BackendQuestion[];
  status: string;
  is_fallback?: boolean;
  fallback_reason?: string | null;
  generation_mode?: string;
  knowledge_base_id?: number | null;
  interview_category?: string | null;
}

interface BackendQuestion {
  question: string;
  category: string;
  answer: string | null;
}

interface BackendSubmitAnswer {
  has_next_question: boolean;
  next_question: BackendQuestion | null;
  new_index: number;
  total_questions: number;
}

interface BackendCurrentQuestion {
  completed: boolean;
  question?: BackendQuestion;
  message?: string;
}

interface BackendReport {
  session_id: string;
  overall_score: number;
  overall_feedback: string;
  category_scores: { category: string; score: number; question_count: number }[];
  question_evaluations: BackendQuestionEvaluation[];
  strengths: string[];
  improvements: string[];
  reference_answers: { question_index: number; question: string; reference_answer: string; key_points: string[] }[];
}

interface BackendQuestionEvaluation {
  question_index: number;
  question: string;
  category: string;
  score: number;
  feedback: string;
  reference_answer: string | null;
  key_points: string[];
}

function fromBackendQuestion(q: BackendQuestion, idx: number) {
  return {
    questionIndex: idx,
    question: q.question,
    type: '',
    category: q.category,
    userAnswer: q.answer,
    score: null,
    feedback: null,
  };
}

function fromBackendSession(s: BackendSession): InterviewSession {
  return {
    sessionId: s.session_id,
    resumeText: s.resume_text,
    totalQuestions: s.total_questions,
    currentQuestionIndex: s.current_index,
    questions: s.questions.map((q, i) => fromBackendQuestion(q, i)),
    status: s.status as InterviewSession['status'],
    isFallback: s.is_fallback,
    fallbackReason: s.fallback_reason,
    generationMode: s.generation_mode,
    knowledgeBaseId: s.knowledge_base_id,
    interviewCategory: s.interview_category,
  };
}

function toBackendCreateRequest(req: CreateInterviewRequest): Record<string, unknown> {
  return {
    resume_text: req.resumeText,
    question_count: req.questionCount,
    resume_id: req.resumeId,
    force_create: req.forceCreate,
    llm_provider: req.llmProvider,
    skill_id: req.skillId,
    difficulty: req.difficulty || 'mid',
    custom_categories: req.customCategories?.map(c => ({
      key: c.key,
      label: c.label,
      priority: c.priority,
      ref: c.ref,
      shared: c.shared,
    })) || [],
    jd_text: req.jdText,
  };
}

export const interviewApi = {
  async listSessions(knowledgeBaseId?: number): Promise<TextSessionMeta[]> {
    const res = await request.get<BackendSessionItem[]>('/api/interview/sessions');
    const items: TextSessionMeta[] = res.map(s => ({
      sessionId: s.session_id,
      skillId: s.skill_id,
      difficulty: s.difficulty,
      resumeId: s.resume_id,
      totalQuestions: s.total_questions,
      status: s.status,
      evaluateStatus: s.evaluate_status,
      evaluateError: s.evaluate_error,
      overallScore: s.overall_score,
      sourceType: null,
      knowledgeBaseId: null,
      interviewCategory: null,
      createdAt: s.created_at,
      completedAt: s.completed_at,
    }));
    if (knowledgeBaseId === undefined) return items;

    const enriched = await Promise.all(items.map(async item => {
      try {
        const session = await request.get<BackendSession>(
          `/api/interview/sessions/${item.sessionId}`
        );
        return {
          ...item,
          sourceType: session.knowledge_base_id ? 'KNOWLEDGE_BASE' : null,
          knowledgeBaseId: session.knowledge_base_id ?? null,
          interviewCategory: session.interview_category ?? null,
        };
      } catch {
        return item;
      }
    }));
    return enriched.filter(item => item.knowledgeBaseId === knowledgeBaseId);
  },

  async createSession(req: CreateInterviewRequest): Promise<InterviewSession> {
    const res = await request.post<BackendSession>('/api/interview/sessions', toBackendCreateRequest(req), {
      timeout: 180000,
    });
    return fromBackendSession(res);
  },

  async getSession(sessionId: string): Promise<InterviewSession> {
    const res = await request.get<BackendSession>(`/api/interview/sessions/${sessionId}`);
    return fromBackendSession(res);
  },

  async getCurrentQuestion(sessionId: string): Promise<CurrentQuestionResponse> {
    const res = await request.get<BackendCurrentQuestion>(`/api/interview/sessions/${sessionId}/question`);
    return {
      completed: res.completed,
      question: res.question ? { questionIndex: 0, ...res.question, type: '', userAnswer: null, score: null, feedback: null } : undefined,
      message: res.message,
    };
  },

  async submitAnswer(req: SubmitAnswerRequest): Promise<SubmitAnswerResponse> {
    const res = await request.post<BackendSubmitAnswer>(
      `/api/interview/sessions/${req.sessionId}/answers`,
      { question_index: req.questionIndex, answer: req.answer },
      { timeout: 180000 }
    );
    return {
      hasNextQuestion: res.has_next_question,
      nextQuestion: res.next_question ? { questionIndex: res.new_index, ...res.next_question, type: '', userAnswer: null, score: null, feedback: null } : null,
      currentIndex: res.new_index,
      totalQuestions: res.total_questions,
    };
  },

  async getReport(sessionId: string): Promise<InterviewReport> {
    const res = await request.get<BackendReport>(`/api/interview/sessions/${sessionId}/report`, {
      timeout: 180000,
    });
    return {
      sessionId: res.session_id,
      totalQuestions: res.category_scores.reduce((sum, c) => sum + c.question_count, 0),
      overallScore: res.overall_score,
      categoryScores: res.category_scores.map(c => ({
        category: c.category,
        score: c.score,
        questionCount: c.question_count,
      })),
      questionDetails: res.question_evaluations.map(q => ({
        questionIndex: q.question_index,
        question: q.question,
        category: q.category,
        userAnswer: '',
        score: q.score,
        feedback: q.feedback,
      })),
      overallFeedback: res.overall_feedback,
      strengths: res.strengths,
      improvements: res.improvements,
      referenceAnswers: res.reference_answers.map(r => ({
        questionIndex: r.question_index,
        question: r.question,
        referenceAnswer: r.reference_answer,
        keyPoints: r.key_points,
      })),
    };
  },

  async findUnfinishedSession(resumeId: number): Promise<InterviewSession | null> {
    try {
      const res = await request.get<BackendSession | null>(`/api/interview/sessions/unfinished/${resumeId}`);
      if (!res) return null;
      return fromBackendSession(res);
    } catch {
      return null;
    }
  },

  async saveAnswer(req: SubmitAnswerRequest): Promise<void> {
    return request.put<void>(
      `/api/interview/sessions/${req.sessionId}/answers`,
      { question_index: req.questionIndex, answer: req.answer }
    );
  },

  async completeInterview(sessionId: string): Promise<void> {
    return request.post<void>(`/api/interview/sessions/${sessionId}/complete`);
  },
};

export const reEvaluateSession = async (sessionId: string): Promise<void> => {
  await request.post<void>(`/api/interview/sessions/${sessionId}/re-evaluate`);
};

const EVALUATION_POLL_INITIAL_DELAY_MS = 3000;
const EVALUATION_POLL_MAX_DELAY_MS = 15000;
const EVALUATION_POLL_BACKOFF_FACTOR = 1.5;
const EVALUATION_POLL_MAX_ATTEMPTS = 80;

export function subscribeEvaluationEvents(
  sessionId: string,
  onStatus: (status: string) => void,
  onCompleted: (overallScore: number, status?: string) => void,
  onFailed: (error: string) => void,
): () => void {
  let es: EventSource | null = null;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;

  const cleanup = () => {
    closed = true;
    if (es) {
      es.close();
      es = null;
    }
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  };

  const startPolling = () => {
    if (pollTimer || closed) return;

    console.warn('[SSE] 降级到轮询兜底');
    let pollCount = 0;
    let pollDelay = EVALUATION_POLL_INITIAL_DELAY_MS;

    const poll = async () => {
      if (closed) return;

      pollCount += 1;
      if (pollCount > EVALUATION_POLL_MAX_ATTEMPTS) {
        cleanup();
        onFailed('评估超时，请手动刷新页面查看结果');
        return;
      }

      try {
        const detail = await historyApi.getInterviewDetail(sessionId);
        if (closed) return;

        const evaluateStatus = detail.evaluateStatus;
        if (evaluateStatus) onStatus(evaluateStatus);

        if (evaluateStatus === 'COMPLETED' || evaluateStatus === 'COMPLETED_WITH_ERRORS' || (!evaluateStatus && detail.status === 'EVALUATED')) {
          cleanup();
          onCompleted(detail.overallScore ?? 0, evaluateStatus);
        } else if (evaluateStatus === 'FAILED') {
          cleanup();
          onFailed(detail.evaluateError || '评估失败');
        }
      } catch (error) {
        console.warn('[POLL] 轮询失败，继续重试', error);
      } finally {
        if (!closed) {
          pollDelay = Math.min(
            Math.round(pollDelay * EVALUATION_POLL_BACKOFF_FACTOR),
            EVALUATION_POLL_MAX_DELAY_MS,
          );
          pollTimer = setTimeout(poll, pollDelay);
        }
      }
    };

    pollTimer = setTimeout(poll, pollDelay);
  };

  try {
    es = new EventSource(`${apiBaseURL}/api/interview/sessions/${sessionId}/evaluation/events`);

    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'completed') {
          cleanup();
          onCompleted(data.overall_score ?? 0, data.evaluate_status);
        } else if (data.type === 'failed') {
          cleanup();
          onFailed(data.error || '评估失败');
        } else if (data.evaluate_status) {
          onStatus(data.evaluate_status);
        }
      } catch (error) {
        console.warn('SSE消息解析失败', error);
      }
    };

    es.onerror = () => {
      if (es) {
        es.close();
        es = null;
      }
      startPolling();
    };

  } catch {
    startPolling();
  }

  return cleanup;
}
