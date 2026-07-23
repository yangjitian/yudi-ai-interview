import { request } from './request';

export type AnalyzeStatus = 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED';
export type EvaluateStatus = 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'COMPLETED_WITH_ERRORS' | 'FAILED';

export interface ResumeListItem {
  id: number;
  filename: string;
  fileSize: number;
  uploadedAt: string;
  accessCount: number;
  latestScore?: number;
  lastAnalyzedAt?: string;
  interviewCount: number;
  analyzeStatus?: AnalyzeStatus;
  analyzeError?: string;
  storageUrl?: string;
}

export interface ResumeStats {
  totalCount: number;
  totalInterviewCount: number;
  totalAccessCount: number;
}

export interface StrengthItem {
  category: string;
  description: string;
}

export interface SuggestionItem {
  category: string;
  priority: '高' | '中' | '低' | string;
  issue: string;
  recommendation: string;
}

export interface AnalysisItem {
  id: number;
  overallScore: number;
  contentScore: number;
  structureScore: number;
  skillMatchScore: number;
  expressionScore: number;
  projectScore: number;
  summary: string;
  analyzedAt: string;
  strengths: StrengthItem[];
  suggestions: SuggestionItem[];
}

export interface InterviewItem {
  id: number;
  sessionId: string;
  totalQuestions: number;
  status: string;
  evaluateStatus?: EvaluateStatus;
  evaluateError?: string;
  overallScore: number | null;
  overallFeedback: string | null;
  createdAt: string;
  completedAt: string | null;
  questions?: unknown[];
  strengths?: string[];
  improvements?: string[];
  referenceAnswers?: ReferenceAnswerItem[];
}

export interface AnswerItem {
  questionIndex: number;
  question: string;
  category: string;
  userAnswer: string;
  score: number | null;
  feedback: string | null;
  referenceAnswer?: string;
  keyPoints?: string[];
  answeredAt?: string;
}

export interface ReferenceAnswerItem {
  questionIndex: number;
  question: string;
  referenceAnswer: string;
  keyPoints: string[];
}

export interface ResumeDetail {
  id: number;
  filename: string;
  fileSize: number;
  contentType: string;
  storageUrl: string;
  uploadedAt: string;
  accessCount: number;
  resumeText: string;
  analyzeStatus?: AnalyzeStatus;
  analyzeError?: string;
  analyses: AnalysisItem[];
  interviews: InterviewItem[];
}

export interface InterviewDetail extends InterviewItem {
  evaluateStatus?: EvaluateStatus;
  evaluateError?: string;
  answers: AnswerItem[];
}

interface BackendStrengthItem {
  category?: string | null;
  description?: string | null;
}

interface BackendSuggestionItem {
  category?: string | null;
  priority?: string | null;
  issue?: string | null;
  recommendation?: string | null;
}

interface BackendAnalysisItem {
  id: number;
  overall_score?: number | null;
  content_score?: number | null;
  structure_score?: number | null;
  skill_match_score?: number | null;
  expression_score?: number | null;
  project_score?: number | null;
  summary?: string | null;
  analyzed_at?: string | null;
  strengths?: BackendStrengthItem[] | null;
  suggestions?: BackendSuggestionItem[] | null;
}

interface BackendReferenceAnswerItem {
  question_index?: number | null;
  question?: string | null;
  reference_answer?: string | null;
  key_points?: string[] | null;
}

interface BackendAnswerItem {
  question_index?: number | null;
  question?: string | null;
  category?: string | null;
  user_answer?: string | null;
  score?: number | null;
  feedback?: string | null;
  reference_answer?: string | null;
  key_points?: string[] | null;
  answered_at?: string | null;
}

interface BackendInterviewReport {
  session_id: string;
  overall_score: number;
  overall_feedback?: string | null;
  category_scores?: unknown[];
  question_evaluations?: BackendAnswerItem[];
  strengths?: string[] | null;
  improvements?: string[] | null;
  reference_answers?: BackendReferenceAnswerItem[] | null;
}

interface BackendInterviewDetail {
  session_id: string;
  skill_id?: string | null;
  difficulty?: string | null;
  total_questions?: number | null;
  current_index?: number | null;
  overall_score?: number | null;
  overall_feedback?: string | null;
  status: string;
  evaluate_status?: EvaluateStatus | null;
  evaluate_error?: string | null;
  questions?: unknown[] | null;
  answers?: BackendAnswerItem[] | null;
  report?: BackendInterviewReport | null;
  created_at?: string | null;
  completed_at?: string | null;
}

interface BackendResumeListItem {
  id: number;
  filename: string;
  file_size?: number | null;
  uploaded_at: string;
  access_count?: number | null;
  latest_score?: number | null;
  last_analyzed_at?: string | null;
  interview_count?: number | null;
  analyze_status?: AnalyzeStatus | null;
  analyze_error?: string | null;
  storage_url?: string | null;
}

interface BackendResumeDetail {
  id: number;
  filename: string;
  file_size?: number | null;
  content_type?: string | null;
  storage_url?: string | null;
  uploaded_at: string;
  access_count?: number | null;
  resume_text?: string | null;
  analyze_status?: AnalyzeStatus | null;
  analyze_error?: string | null;
  analyses?: BackendAnalysisItem[] | null;
  interviews?: InterviewItem[] | null;
}

function mapStrengthItem(item: BackendStrengthItem): StrengthItem {
  return {
    category: item.category || '亮点',
    description: item.description || '',
  };
}

function mapSuggestionItem(item: BackendSuggestionItem): SuggestionItem {
  return {
    category: item.category || '综合',
    priority: item.priority || '中',
    issue: item.issue || '',
    recommendation: item.recommendation || '',
  };
}

function mapReferenceAnswerItem(item: BackendReferenceAnswerItem): ReferenceAnswerItem {
  return {
    questionIndex: item.question_index || 0,
    question: item.question || '',
    referenceAnswer: item.reference_answer || '',
    keyPoints: item.key_points || [],
  };
}

function mapAnswerItem(item: BackendAnswerItem): AnswerItem {
  return {
    questionIndex: item.question_index || 0,
    question: item.question || '',
    category: item.category || '',
    userAnswer: item.user_answer || '',
    score: item.score ?? null,
    feedback: item.feedback ?? null,
    referenceAnswer: item.reference_answer || undefined,
    keyPoints: item.key_points || [],
    answeredAt: item.answered_at || undefined,
  };
}

function mapAnalysisItem(item: BackendAnalysisItem): AnalysisItem {
  return {
    id: item.id,
    overallScore: item.overall_score || 0,
    contentScore: item.content_score || 0,
    structureScore: item.structure_score || 0,
    skillMatchScore: item.skill_match_score || 0,
    expressionScore: item.expression_score || 0,
    projectScore: item.project_score || 0,
    summary: item.summary || '',
    analyzedAt: item.analyzed_at || '',
    strengths: (item.strengths || []).map(mapStrengthItem),
    suggestions: (item.suggestions || []).map(mapSuggestionItem),
  };
}

function mapResumeListItem(item: BackendResumeListItem): ResumeListItem {
  return {
    id: item.id,
    filename: item.filename,
    fileSize: item.file_size || 0,
    uploadedAt: item.uploaded_at,
    accessCount: item.access_count || 0,
    latestScore: item.latest_score ?? undefined,
    lastAnalyzedAt: item.last_analyzed_at || undefined,
    interviewCount: item.interview_count || 0,
    analyzeStatus: item.analyze_status || undefined,
    analyzeError: item.analyze_error || undefined,
    storageUrl: item.storage_url || undefined,
  };
}

function mapResumeDetail(item: BackendResumeDetail): ResumeDetail {
  return {
    id: item.id,
    filename: item.filename,
    fileSize: item.file_size || 0,
    contentType: item.content_type || '',
    storageUrl: item.storage_url || '',
    uploadedAt: item.uploaded_at,
    accessCount: item.access_count || 0,
    resumeText: item.resume_text || '',
    analyzeStatus: item.analyze_status || undefined,
    analyzeError: item.analyze_error || undefined,
    analyses: (item.analyses || []).map(mapAnalysisItem),
    interviews: item.interviews || [],
  };
}

function mapInterviewDetail(item: BackendInterviewDetail): InterviewDetail {
  const report = item.report;
  return {
    id: 0,
    sessionId: item.session_id,
    totalQuestions: item.total_questions || 0,
    status: item.status,
    evaluateStatus: item.evaluate_status || undefined,
    evaluateError: item.evaluate_error || undefined,
    overallScore: item.overall_score ?? report?.overall_score ?? null,
    overallFeedback: item.overall_feedback ?? report?.overall_feedback ?? null,
    createdAt: item.created_at || '',
    completedAt: item.completed_at || null,
    questions: item.questions || [],
    strengths: report?.strengths || [],
    improvements: report?.improvements || [],
    referenceAnswers: (report?.reference_answers || []).map(mapReferenceAnswerItem),
    answers: (item.answers || []).map(mapAnswerItem),
  };
}

export const historyApi = {
  /**
   * 获取所有简历列表
   */
  async getResumes(): Promise<ResumeListItem[]> {
    const res = await request.get<BackendResumeListItem[]>('/api/resumes');
    return res.map(mapResumeListItem);
  },

  /**
   * 获取简历详情
   */
  async getResumeDetail(id: number): Promise<ResumeDetail> {
    const res = await request.get<BackendResumeDetail>(`/api/resumes/${id}/detail`);
    return mapResumeDetail(res);
  },

  /**
   * 获取面试详情
   */
  async getInterviewDetail(sessionId: string): Promise<InterviewDetail> {
    const res = await request.get<BackendInterviewDetail>(`/api/interview/sessions/${sessionId}/details`);
    return mapInterviewDetail(res);
  },

  /**
   * 导出简历分析报告PDF
   */
  async exportAnalysisPdf(resumeId: number): Promise<Blob> {
    const response = await request.get<Blob>(`/api/resumes/${resumeId}/export`, {
      responseType: 'blob',
      skipResultTransform: true,
    } as never);
    return response;
  },

  /**
   * 导出面试报告PDF
   */
  async exportInterviewPdf(sessionId: string): Promise<Blob> {
    const response = await request.get<Blob>(`/api/interview/sessions/${sessionId}/export`, {
      responseType: 'blob',
      skipResultTransform: true,
    } as never);
    return response;
  },

  /**
   * 删除简历
   */
  async deleteResume(id: number): Promise<void> {
    return request.delete(`/api/resumes/${id}`);
  },

  /**
   * 删除面试记录
   */
  async deleteInterview(sessionId: string): Promise<void> {
    return request.delete(`/api/interview/sessions/${sessionId}`);
  },

  /**
   * 获取简历统计信息
   */
  async getStatistics(): Promise<ResumeStats> {
    return request.get<ResumeStats>('/api/resumes/statistics');
  },

  /**
   * 重新分析简历
   */
  async reanalyze(id: number): Promise<void> {
    return request.post(`/api/resumes/${id}/reanalyze`);
  },
};
