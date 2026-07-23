import { request } from './request';
import type { UploadResponse } from '../types/resume';

export interface ResumeListItem {
  id: number;
  filename: string;
  fileSize: number;
  uploadedAt: string;
  accessCount: number;
  latestScore?: number;
  lastAnalyzedAt?: string;
  interviewCount: number;
  analyzeStatus?: string;
  analyzeError?: string;
  storageUrl?: string;
}

export interface ResumeDetail extends ResumeListItem {
  contentType: string;
  resumeText: string;
  analyses: unknown[];
  interviews: unknown[];
}

export interface ResumeStatistics {
  totalCount: number;
  totalAccessCount: number;
  totalInterviewCount: number;
}

export const resumeApi = {
  async uploadAndAnalyze(file: File): Promise<UploadResponse> {
    const formData = new FormData();
    formData.append('file', file);
    return request.upload<UploadResponse>('/api/resumes/upload', formData);
  },

  async healthCheck(): Promise<{ status: string; service: string }> {
    return request.get('/api/resumes/health');
  },

  async list(): Promise<ResumeListItem[]> {
    return request.get<ResumeListItem[]>('/api/resumes');
  },

  async getDetail(id: number): Promise<ResumeDetail> {
    return request.get<ResumeDetail>(`/api/resumes/${id}/detail`);
  },

  async getStatistics(): Promise<ResumeStatistics> {
    return request.get<ResumeStatistics>('/api/resumes/statistics');
  },

  async delete(id: number): Promise<void> {
    return request.delete<void>(`/api/resumes/${id}`);
  },

  async reanalyze(id: number): Promise<void> {
    return request.post<void>(`/api/resumes/${id}/reanalyze`);
  },
};
