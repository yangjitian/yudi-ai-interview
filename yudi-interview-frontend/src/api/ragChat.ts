import { request, getErrorMessage } from './request';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

interface BackendSession {
  id: number;
  title: string;
  is_pinned: boolean;
  message_count: number;
  knowledge_base_names: string[];
  created_at: string;
  updated_at?: string;
}

interface BackendSessionDetail {
  id: number;
  title: string;
  is_pinned: boolean;
  knowledge_bases: { id: number; name: string; category?: string | null; vector_status?: string; chunk_count?: number | null }[];
  messages: { id: number; role: string; content: string; created_at: string }[];
  created_at: string;
}

interface BackendCreateSession {
  session_id: number;
  title: string;
}

export interface RagChatSession {
  id: number;
  title: string;
  knowledgeBaseIds: number[];
  createdAt: string;
}

export interface RagChatSessionListItem {
  id: number;
  title: string;
  messageCount: number;
  knowledgeBaseNames: string[];
  updatedAt: string;
  isPinned: boolean;
}

export interface RagChatMessage {
  id: number;
  type: 'user' | 'assistant';
  content: string;
  createdAt: string;
}

export interface KnowledgeBaseItem {
  id: number;
  name: string;
  originalFilename: string;
  fileSize: number;
  contentType: string;
  uploadedAt: string;
  lastAccessedAt: string;
  accessCount: number;
  questionCount: number;
}

export interface RagChatSessionDetail {
  id: number;
  title: string;
  knowledgeBases: KnowledgeBaseItem[];
  messages: RagChatMessage[];
  createdAt: string;
  updatedAt: string;
}

function fromBackendSession(s: BackendSession): RagChatSessionListItem {
  return {
    id: s.id,
    title: s.title,
    messageCount: s.message_count,
    knowledgeBaseNames: s.knowledge_base_names || [],
    updatedAt: s.updated_at || s.created_at,
    isPinned: s.is_pinned,
  };
}

function fromBackendSessionDetail(s: BackendSessionDetail): RagChatSessionDetail {
  return {
    id: s.id,
    title: s.title,
    knowledgeBases: s.knowledge_bases.map(kb => ({
      id: kb.id,
      name: kb.name,
      originalFilename: '',
      fileSize: 0,
      contentType: '',
      uploadedAt: '',
      lastAccessedAt: '',
      accessCount: 0,
      questionCount: 0,
    })),
    messages: s.messages.map(m => ({
      id: m.id,
      type: m.role === 'user' ? 'user' : 'assistant',
      content: m.content,
      createdAt: m.created_at,
    })),
    createdAt: s.created_at,
    updatedAt: s.created_at,
  };
}

export const ragChatApi = {
  async createSession(knowledgeBaseIds: number[], title?: string): Promise<RagChatSession> {
    const res = await request.post<BackendCreateSession>('/api/rag-chat/sessions', {
      title,
      knowledge_base_ids: knowledgeBaseIds,
    });
    return {
      id: res.session_id,
      title: res.title,
      knowledgeBaseIds,
      createdAt: new Date().toISOString(),
    };
  },

  async listSessions(): Promise<RagChatSessionListItem[]> {
    const res = await request.get<BackendSession[]>('/api/rag-chat/sessions');
    return res.map(fromBackendSession);
  },

  async getSessionDetail(sessionId: number): Promise<RagChatSessionDetail> {
    const res = await request.get<BackendSessionDetail>(`/api/rag-chat/sessions/${sessionId}`);
    return fromBackendSessionDetail(res);
  },

  async updateSessionTitle(sessionId: number, title: string): Promise<void> {
    return request.put(`/api/rag-chat/sessions/${sessionId}/title`, { title });
  },

  async updateKnowledgeBases(_sessionId: number, _knowledgeBaseIds: number[]): Promise<void> {
    return request.put(`/api/rag-chat/sessions/${_sessionId}/knowledge-bases`, {
      knowledge_base_ids: _knowledgeBaseIds,
    });
  },

  async togglePin(sessionId: number): Promise<void> {
    return request.put(`/api/rag-chat/sessions/${sessionId}/pin`);
  },

  async deleteSession(sessionId: number): Promise<void> {
    return request.delete(`/api/rag-chat/sessions/${sessionId}`);
  },

  async sendMessage(sessionId: number, question: string): Promise<{ answer: string }> {
    return request.post(`/api/rag-chat/sessions/${sessionId}/messages`, { question });
  },

  async getMessages(sessionId: number): Promise<RagChatMessage[]> {
    const messages = await request.get<BackendSessionDetail['messages']>(
      `/api/rag-chat/sessions/${sessionId}/messages`,
    );
    return messages.map(message => ({
      id: message.id,
      type: message.role === 'user' ? 'user' : 'assistant',
      content: message.content,
      createdAt: message.created_at,
    }));
  },

  async sendMessageStream(
    sessionId: number,
    question: string,
    onMessage: (chunk: string) => void,
    onComplete: () => void,
    onError: (error: Error) => void,
  ): Promise<void> {
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/rag-chat/sessions/${sessionId}/messages/stream`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question }),
        },
      );

      if (!response.ok) {
        try {
          const errorData = await response.json();
          if (errorData && errorData.message) {
            throw new Error(errorData.message);
          }
        } catch {
          // ignore parse failure and use HTTP status fallback below
        }
        throw new Error(`请求失败 (${response.status})`);
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error('无法获取响应流');
      }

      const decoder = new TextDecoder();
      let buffer = '';

      const extractEventContent = (event: string): string | null => {
        if (!event.trim()) return null;
        const lines = event.split('\n');
        const contentParts: string[] = [];
        for (const line of lines) {
          if (line.startsWith('data:')) {
            contentParts.push(line.substring(5));
          }
        }
        if (contentParts.length === 0) return null;
        return contentParts.join('').replace(/\\n/g, '\n').replace(/\\r/g, '\r');
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          if (buffer) {
            const content = extractEventContent(buffer);
            if (content) onMessage(content);
          }
          onComplete();
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        let newlineIndex = buffer.indexOf('\n\n');
        if (newlineIndex === -1) {
          const singleLineIndex = buffer.indexOf('\n');
          if (singleLineIndex !== -1 && buffer.substring(0, singleLineIndex).startsWith('data:')) {
            const line = buffer.substring(0, singleLineIndex);
            const content = extractEventContent(line);
            if (content) onMessage(content);
            buffer = buffer.substring(singleLineIndex + 1);
          }
          continue;
        }

        const eventBlock = buffer.substring(0, newlineIndex);
        buffer = buffer.substring(newlineIndex + 2);
        const content = extractEventContent(eventBlock);
        if (content !== null) onMessage(content);
      }
    } catch (error) {
      onError(new Error(getErrorMessage(error)));
    }
  },
};
