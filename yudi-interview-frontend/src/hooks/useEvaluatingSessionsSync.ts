import { useEffect, useRef } from 'react';
import { subscribeEvaluationEvents } from '../api/interview';
import { subscribeVoiceEvaluationEvents } from '../api/voiceInterview';

interface SessionLike {
  sessionId: string;
  evaluateStatus?: string;
  type?: 'text' | 'voice';
  [key: string]: unknown;
}

const EVALUATING_STATUSES = new Set(['PENDING', 'PROCESSING']);
const MAX_CONCURRENT_SSE = 5;

/**
 * 对列表中所有「评估中」的 session 建立 SSE 监听，
 * 收到 completed/failed 时通过 onStatusChange 更新该条目状态。
 *
 * 设计原则：
 * - 只对 PENDING / PROCESSING 状态的 session 建立 SSE
 * - 每个 session 一个连接，完成后自动关闭
 * - 并发连接上限 MAX_CONCURRENT_SSE，超出后等待其他连接释放
 * - 组件 unmount 或 sessions 变化时，清理不再需要的连接
 */
export function useEvaluatingSessionsSync<T extends SessionLike>(
  sessions: T[],
  onStatusChange: (sessionId: string, newStatus: string, overallScore?: number) => void,
): void {
  const cleanupMapRef = useRef<Map<string, () => void>>(new Map());
  const onStatusChangeRef = useRef(onStatusChange);
  onStatusChangeRef.current = onStatusChange;
  const evaluatingSessionIds = sessions
    .filter((session) => session.sessionId && EVALUATING_STATUSES.has(session.evaluateStatus ?? ''))
    .map((session) => session.sessionId)
    .sort()
    .join(',');

  useEffect(() => {
    const toWatch = sessions.filter(
      (s) => s.sessionId && EVALUATING_STATUSES.has(s.evaluateStatus ?? ''),
    );
    const toWatchIds = new Set(toWatch.map((s) => s.sessionId));

    // 清理不再需要监听的连接（已完成 / 已从列表移除）
    cleanupMapRef.current.forEach((cleanup, sessionId) => {
      if (!toWatchIds.has(sessionId)) {
        cleanup();
        cleanupMapRef.current.delete(sessionId);
      }
    });

    // 计算可订阅槽位
    const activeCount = cleanupMapRef.current.size;
    const remainingSlots = Math.max(0, MAX_CONCURRENT_SSE - activeCount);
    const toSubscribe = toWatch
      .filter((s) => !cleanupMapRef.current.has(s.sessionId))
      .slice(0, remainingSlots);

    toSubscribe.forEach((session) => {
      let cleanup = () => {};
      if (session.type === 'voice') {
        cleanup = subscribeVoiceEvaluationEvents(
          session.sessionId,
          (response) => {
            const status = response.evaluateStatus;
            if (!status) return;
            onStatusChangeRef.current(
              session.sessionId,
              status,
              response.evaluation?.overallScore,
            );
            if (status === 'COMPLETED' || status === 'COMPLETED_WITH_ERRORS' || status === 'FAILED') {
              cleanupMapRef.current.delete(session.sessionId);
            }
          },
        );
      } else {
        cleanup = subscribeEvaluationEvents(
        session.sessionId,
        (status) => {
          onStatusChangeRef.current(session.sessionId, status);
        },
        (overallScore, status) => {
          onStatusChangeRef.current(session.sessionId, status || 'COMPLETED', overallScore);
          cleanupMapRef.current.delete(session.sessionId);
          cleanup();
        },
        (_error) => {
          onStatusChangeRef.current(session.sessionId, 'FAILED');
          cleanupMapRef.current.delete(session.sessionId);
          cleanup();
        },
        );
      }
      cleanupMapRef.current.set(session.sessionId, cleanup);
    });

    return () => {
      cleanupMapRef.current.forEach((cleanup) => cleanup());
      cleanupMapRef.current.clear();
    };
    // 仅依赖 sessions 的标识和状态变化，避免 onStatusChange 变化触发重订阅
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [evaluatingSessionIds]);
}
