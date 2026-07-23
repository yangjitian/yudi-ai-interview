import { getErrorMessage, request } from './request';

export type VectorStatus = 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED';

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

  async getAllKnowledgeBases(sortBy?: SortOption): Promise<KnowledgeBaseItem[]> {
    const res = await request.get<BackendKBItem[]>('/api/knowledge-base');
    let items = res.map(fromBackendKBItem);
    if (sortBy === 'size') items = [...items].sort((a, b) => b.fileSize - a.fileSize);
    else if (sortBy === 'access') items = [...items].sort((a, b) => b.accessCount - a.accessCount);
    else if (sortBy === 'question') items = [...items].sort((a, b) => b.questionCount - a.questionCount);
    return items;
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
