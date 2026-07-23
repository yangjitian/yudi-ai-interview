import { useState } from 'react';
import { resumeApi } from '../api/resume';
import { getErrorMessage } from '../api/request';
import FileUploadCard from '../components/FileUploadCard';

interface UploadPageProps {
  onUploadComplete: (resumeId: number) => void;
}

export default function UploadPage({ onUploadComplete }: UploadPageProps) {
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');

  const handleUpload = async (file: File) => {
    setUploading(true);
    setError('');

    try {
      const data = await resumeApi.uploadAndAnalyze(file);

      if (!data.resume_id) {
        throw new Error('上传失败，请重试');
      }

      onUploadComplete(data.resume_id);
    } catch (err) {
      setError(getErrorMessage(err));
      setUploading(false);
    }
  };

  return (
    <FileUploadCard
      title="开始您的 AI 模拟面试"
      subtitle="上传 PDF 或 Word 简历，AI 将为您定制专属面试方案"
      accept=".pdf,.doc,.docx,.txt"
      formatHint="支持 PDF, DOCX, TXT"
      maxSizeHint="最大 10MB"
      uploading={uploading}
      uploadButtonText="开始上传"
      selectButtonText="选择简历文件"
      error={error}
      onUpload={handleUpload}
    />
  );
}
