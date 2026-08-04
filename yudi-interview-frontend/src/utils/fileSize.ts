export const RESUME_MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const KB_MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

export function formatBytesToMB(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(0)}MB`;
}

const UPLOAD_SIZE_MESSAGE_PATTERN = /文件大小不能超过 (\d+) 字节/;

export function formatUploadSizeMessage(message: string): string {
  const match = message.match(UPLOAD_SIZE_MESSAGE_PATTERN);
  if (!match) {
    return message;
  }
  const bytes = Number(match[1]);
  return `文件大小不能超过 ${formatBytesToMB(bytes)}`;
}
