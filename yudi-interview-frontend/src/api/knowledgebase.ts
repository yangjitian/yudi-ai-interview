import { getErrorMessage, request } from './request';
import type { InterviewSession } from '../types/interview';

export type VectorStatus = 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED';
export type QuestionGenStatus = 'NONE' | 'QUEUED' | 'PROCESSING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';

interface BackendKBItem {
  id: number;
  name: string;
  category: string | null;
  original_filename: string;
  file_size: number | null;
  content_type: string | null;
  uploaded_at: string;
  last_accessed_at: string | null;
  access_count: number;
  question_count: number;
  vector_status: string;
  vector_error: string | null;
  chunk_count: number | null;
}

interface UpdateCategoryRequest {
  category: string | null;
}

function fromBackendKBItem(b: BackendKBItem): KnowledgeBaseItem {
  return {
    id: b.id,
    name: b.name,
    category: b.category,
    originalFilename: b.original_filename,
    fileSize: b.file_size || 0,
    contentType: b.content_type || '',
    uploadedAt: b.uploaded_at,
    lastAccessedAt: b.last_accessed_at || '',
    accessCount: b.access_count,
    questionCount: b.question_count,
    vectorStatus: b.vector_status as VectorStatus,
    vectorError: b.vector_error,
    chunkCount: b.chunk_count || 0,
    questionGenStatus: 'NONE',
    questionGenError: null,
  };
}

export interface KnowledgeBaseItem {
  id: number;
  name: string;
  category: string | null;
  originalFilename: string;
  fileSize: number;
  contentType: string;
  uploadedAt: string;
  lastAccessedAt: string;
  accessCount: number;
  questionCount: number;
  vectorStatus: VectorStatus;
  vectorError: string | null;
  chunkCount: number;
  questionGenStatus: QuestionGenStatus;
  questionGenError: string | null;
}

export interface KnowledgeBaseStats {
  totalCount: number;
  totalQuestionCount: number;
  totalAccessCount: number;
  completedCount: number;
  processingCount: number;
}

export type SortOption = 'time' | 'size' | 'access' | 'question';

export interface UploadKnowledgeBaseResponse {
  id: number;
  name: string;
  duplicate: boolean;
}

export interface QueryRequest {
  knowledgeBaseIds: number[];
  question: string;
}

export interface QueryResponse {
  answer: string;
  chunks?: { content: string; score: number; source: string }[];
}

export interface KnowledgeBaseQuestionFollowUp {
  question: string;
  referenceAnswer?: string | null;
  keyPoints?: string[];
  scoringRubric?: string | null;
}

export type KnowledgeBaseQuestionStatus = 'DRAFT' | 'ACTIVE' | 'ARCHIVED' | 'STALE';

export interface KnowledgeBaseQuestion {
  id: number;
  knowledgeBaseId: number;
  knowledgeBaseName: string;
  skillId: string;
  difficulty: string;
  type: string | null;
  category: string;
  question: string;
  topicSummary: string | null;
  referenceAnswer: string | null;
  keyPoints: string[];
  scoringRubric: string | null;
  followUps: KnowledgeBaseQuestionFollowUp[];
  sourceContext: string | null;
  status: KnowledgeBaseQuestionStatus;
  createdAt: string;
  updatedAt: string;
}

export interface GenerateKnowledgeBaseQuestionsRequest {
  difficulty?: string;
  questionCount: number;
  followUpCount?: number;
  categoryLimit: number;
  llmProvider?: string;
}

export interface QuestionGenerationConfig {
  difficulty: string;
  questionCount: number;
  followUpCount: number;
  categoryLimit: number;
  llmProvider: string | null;
}

export interface QuestionGenStatusResponse {
  knowledgeBaseId: number;
  questionGenStatus: QuestionGenStatus;
  questionGenTaskId: string | null;
  questionGenConfig: QuestionGenerationConfig | null;
  savedCount: number;
  skippedCount: number;
  message: string | null;
  error: string | null;
  updatedAt: string | null;
}

export interface SaveKnowledgeBaseQuestionRequest {
  difficulty?: string;
  type?: string | null;
  category: string;
  question: string;
  topicSummary?: string | null;
  referenceAnswer?: string | null;
  keyPoints?: string[];
  scoringRubric?: string | null;
  followUps?: KnowledgeBaseQuestionFollowUp[];
  sourceContext?: string | null;
  status?: KnowledgeBaseQuestionStatus;
}

export interface ListKnowledgeBaseQuestionsParams {
  status?: KnowledgeBaseQuestionStatus | '';
  category?: string;
  difficulty?: string;
  keyword?: string;
}

export interface CategoryCount {
  category: string;
  count: number;
}

export interface CreateKnowledgeBaseInterviewRequest {
  knowledgeBaseId: number;
  category?: string;
  difficulty?: string;
  mainQuestionCount: number;
  followUpCount: number;
  llmProvider?: string;
}

export interface InterviewCategoryCapacity {
  category: string;
  availableQuestionCount: number;
}

export interface InterviewFollowUpCapacity {
  followUpCount: number;
  availableQuestionCount: number;
  selectable: boolean;
}

export interface KnowledgeBaseInterviewCapacityResponse {
  knowledgeBaseId: number;
  category: string | null;
  difficulty: string;
  mainQuestionCount: number;
  categories: InterviewCategoryCapacity[];
  followUpOptions: InterviewFollowUpCapacity[];
}

export interface GetKnowledgeBaseInterviewCapacityParams {
  category?: string;
  difficulty: string;
  mainQuestionCount: number;
}

async function fetchQuestionGenerationStatus(id: number): Promise<QuestionGenStatusResponse> {
  return request.get<QuestionGenStatusResponse>(
    `/api/knowledgebase/${id}/questions/generation-status`
  );
}

export const knowledgeBaseApi = {
  async uploadKnowledgeBase(file: File, name?: string, category?: string): Promise<UploadKnowledgeBaseResponse> {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('name', name || file.name);
    if (category) {
      formData.append('category', category);
    }
    return request.upload<UploadKnowledgeBaseResponse>('/api/knowledge-base/upload', formData);
  },

  async downloadKnowledgeBase(id: number): Promise<Blob> {
    return request.get<Blob>(`/api/knowledge-base/${id}/download`, {
      responseType: 'blob',
      skipResultTransform: true,
    } as never);
  },

  async getAllKnowledgeBases(sortBy?: SortOption, vectorStatus?: VectorStatus): Promise<KnowledgeBaseItem[]> {
    const res = await request.get<BackendKBItem[]>('/api/knowledge-base');
    let items = res.map(fromBackendKBItem);
    if (vectorStatus) items = items.filter(item => item.vectorStatus === vectorStatus);
    if (sortBy === 'size') items = [...items].sort((a, b) => b.fileSize - a.fileSize);
    else if (sortBy === 'access') items = [...items].sort((a, b) => b.accessCount - a.accessCount);
    else if (sortBy === 'question') items = [...items].sort((a, b) => b.questionCount - a.questionCount);
    return Promise.all(items.map(async item => {
      try {
        const status = await fetchQuestionGenerationStatus(item.id);
        return {
          ...item,
          questionGenStatus: status.questionGenStatus,
          questionGenError: status.error,
        };
      } catch {
        return item;
      }
    }));
  },

  async getKnowledgeBase(id: number): Promise<KnowledgeBaseItem> {
    const res = await request.get<BackendKBItem>(`/api/knowledge-base/${id}`);
    return fromBackendKBItem(res);
  },

  async deleteKnowledgeBase(id: number): Promise<void> {
    return request.delete(`/api/knowledge-base/${id}`);
  },

  async getAllCategories(): Promise<string[]> {
    return request.get<string[]>('/api/knowledge-base/categories');
  },

  async getByCategory(category: string): Promise<KnowledgeBaseItem[]> {
    const res = await request.get<BackendKBItem[]>(`/api/knowledge-base/category/${encodeURIComponent(category)}`);
    return res.map(fromBackendKBItem);
  },

  async getUncategorized(): Promise<KnowledgeBaseItem[]> {
    const res = await request.get<BackendKBItem[]>('/api/knowledge-base/uncategorized');
    return res.map(fromBackendKBItem);
  },

  async updateCategory(id: number, category: string | null): Promise<void> {
    return request.put<void>(`/api/knowledge-base/${id}/category`, { category } satisfies UpdateCategoryRequest);
  },

  async search(keyword: string): Promise<KnowledgeBaseItem[]> {
    const res = await request.get<BackendKBItem[]>('/api/knowledge-base/search', {
      params: { keyword },
    });
    return res.map(fromBackendKBItem);
  },

  async getStatistics(): Promise<KnowledgeBaseStats> {
    return request.get<KnowledgeBaseStats>('/api/knowledge-base/stats');
  },

  async revectorize(id: number): Promise<void> {
    return request.post(`/api/knowledge-base/${id}/revectorize`);
  },

  async generateQuestions(
    id: number,
    req: GenerateKnowledgeBaseQuestionsRequest,
  ): Promise<QuestionGenStatusResponse> {
    return request.post<QuestionGenStatusResponse>(
      `/api/knowledgebase/${id}/questions/generate`,
      req,
    );
  },

  async getQuestionGenerationStatus(id: number): Promise<QuestionGenStatusResponse> {
    return fetchQuestionGenerationStatus(id);
  },

  async cancelQuestionGeneration(id: number): Promise<QuestionGenStatusResponse> {
    return request.post<QuestionGenStatusResponse>(
      `/api/knowledgebase/${id}/questions/generation/cancel`,
    );
  },

  async listQuestions(
    id: number,
    params?: ListKnowledgeBaseQuestionsParams,
  ): Promise<KnowledgeBaseQuestion[]> {
    return request.get<KnowledgeBaseQuestion[]>(`/api/knowledge-base/${id}/questions`, {
      params,
    });
  },

  async listCategories(id: number): Promise<CategoryCount[]> {
    return request.get<CategoryCount[]>(`/api/knowledge-base/${id}/questions/categories`);
  },

  async createQuestion(
    id: number,
    req: SaveKnowledgeBaseQuestionRequest,
  ): Promise<KnowledgeBaseQuestion> {
    return request.post<KnowledgeBaseQuestion>(`/api/knowledge-base/${id}/questions`, req);
  },

  async updateQuestion(
    id: number,
    req: Partial<SaveKnowledgeBaseQuestionRequest>,
  ): Promise<KnowledgeBaseQuestion> {
    return request.put<KnowledgeBaseQuestion>(`/api/knowledge-base/questions/${id}`, req);
  },

  async updateQuestionStatus(
    id: number,
    status: KnowledgeBaseQuestionStatus,
  ): Promise<KnowledgeBaseQuestion> {
    return request.put<KnowledgeBaseQuestion>(
      `/api/knowledge-base/questions/${id}/status`,
      { status },
    );
  },

  async deleteQuestion(id: number): Promise<void> {
    return request.delete(`/api/knowledge-base/questions/${id}`);
  },

  async createInterviewSession(
    req: CreateKnowledgeBaseInterviewRequest,
  ): Promise<InterviewSession> {
    return request.post<InterviewSession>('/api/knowledge-base-interviews/sessions', req);
  },

  async getInterviewCapacity(
    id: number,
    params: GetKnowledgeBaseInterviewCapacityParams,
  ): Promise<KnowledgeBaseInterviewCapacityResponse> {
    return request.get<KnowledgeBaseInterviewCapacityResponse>(
      `/api/knowledge-base/${id}/interview-capacity`,
      {
        params: {
          category: params.category,
          difficulty: params.difficulty,
          mainQuestionCount: params.mainQuestionCount,
        },
      },
    );
  },

  async queryKnowledgeBase(req: QueryRequest): Promise<QueryResponse> {
    return request.post<QueryResponse>('/api/knowledge-base/query', {
      query_text: req.question,
      knowledge_base_ids: req.knowledgeBaseIds,
      top_k: 5,
    }, {
      timeout: 180000,
    });
  },

  async queryKnowledgeBaseStream(
    req: QueryRequest,
    onMessage: (chunk: string) => void,
    onComplete: () => void,
    onError: (error: Error) => void,
  ): Promise<void> {
    try {
      const result = await this.queryKnowledgeBase(req);
      onMessage(result.answer);
      onComplete();
    } catch (error) {
      onError(new Error(getErrorMessage(error)));
    }
  },
};
