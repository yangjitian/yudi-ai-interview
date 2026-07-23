import { useRef, useState, useEffect, useCallback } from 'react';
// @ts-ignore - vad is loaded via script tag
import { Loader2, Mic, MicOff } from 'lucide-react';

// Declare global vad object (loaded via script tag in index.html)
declare global {
  interface Window {
    vad: {
      MicVAD: {
        new: (config: any) => Promise<{
          start: () => Promise<void>;
          pause: () => void;
          destroy: () => void;
        }>;
      };
    };
  }
}

interface AudioRecorderProps {
  isRecording: boolean;
  disabled?: boolean;
  onRecordingChange: (isRecording: boolean) => void;
  onRecordingStopped?: () => void;
  onAudioData: (audioData: string) => void;
  onSpeechStart?: () => void;
  onSpeechEnd?: () => void;
  onPermissionDenied?: (message: string) => void;
}

export default function AudioRecorder({
  isRecording,
  disabled = false,
  onRecordingChange,
  onRecordingStopped,
  onAudioData,
  onSpeechStart,
  onSpeechEnd,
  onPermissionDenied,
}: AudioRecorderProps) {
  const [volume, setVolume] = useState(0);
  const [isStarting, setIsStarting] = useState(false);
  const [isStopping, setIsStopping] = useState(false);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const sourceNodeRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const vadRef = useRef<any>(null);
  const workletNodeRef = useRef<AudioWorkletNode | null>(null);
  const gainNodeRef = useRef<GainNode | null>(null);
  const mountedRef = useRef(true);
  const recordingActiveRef = useRef(false);
  const startingRef = useRef(false);
  const stoppingRef = useRef(false);

  const TARGET_SAMPLE_RATE = 16000;

  const isPermissionDeniedError = (error: unknown): boolean => {
    if (error instanceof DOMException) {
      return error.name === 'NotAllowedError' || error.name === 'PermissionDeniedError';
    }
    if (error instanceof Error) {
      return error.name === 'NotAllowedError' || error.name === 'PermissionDeniedError';
    }
    return false;
  };

  const flushPendingAudio = useCallback(async () => {
    const workletNode = workletNodeRef.current;
    if (!workletNode || !recordingActiveRef.current || !mountedRef.current) {
      return;
    }

    await new Promise<void>((resolve) => {
      const originalHandler = workletNode.port.onmessage;
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeoutId);
        workletNode.port.onmessage = originalHandler;
        resolve();
      };
      const timeoutId = window.setTimeout(finish, 250);

      workletNode.port.onmessage = (event) => {
        if (event.data?.type === 'flushed') {
          finish();
          return;
        }
        originalHandler?.call(workletNode.port, event);
      };
      workletNode.port.postMessage({ type: 'flush' });
    });
  }, []);

  const cleanupRecordingResources = useCallback(async (updateVolume = true) => {
    if (vadRef.current) {
      try {
        vadRef.current.pause();
        vadRef.current.destroy?.();
      } catch (_e) { /* ignore cleanup errors */ }
      vadRef.current = null;
    }

    if (sourceNodeRef.current) {
      try {
        sourceNodeRef.current.disconnect();
      } catch (_e) { /* ignore disconnect errors */ }
      sourceNodeRef.current = null;
    }

    await flushPendingAudio();
    recordingActiveRef.current = false;

    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }

    if (workletNodeRef.current) {
      try {
        workletNodeRef.current.port.onmessage = null;
        workletNodeRef.current.disconnect();
      } catch (_e) { /* ignore disconnect errors */ }
      workletNodeRef.current = null;
    }

    if (gainNodeRef.current) {
      try {
        gainNodeRef.current.disconnect();
      } catch (_e) { /* ignore disconnect errors */ }
      gainNodeRef.current = null;
    }

    if (analyserRef.current) {
      try {
        analyserRef.current.disconnect();
      } catch (_e) { /* ignore disconnect errors */ }
      analyserRef.current = null;
    }

    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach(track => track.stop());
      mediaStreamRef.current = null;
    }

    if (audioContextRef.current) {
      try {
        audioContextRef.current.close();
      } catch (_e) { /* ignore close errors */ }
      audioContextRef.current = null;
    }

    if (updateVolume) {
      setVolume(0);
    }
  }, [flushPendingAudio]);

  const arrayBufferToBase64 = (buffer: ArrayBuffer): string => {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    const chunkSize = 0x8000;
    for (let i = 0; i < bytes.length; i += chunkSize) {
      const chunk = bytes.subarray(i, i + chunkSize);
      binary += String.fromCharCode(...chunk);
    }
    return btoa(binary);
  };

  const startRecording = async () => {
    if (startingRef.current || stoppingRef.current) return;
    startingRef.current = true;
    setIsStarting(true);
    const totalStartedAt = performance.now();

    try {
      if (!window.AudioContext) {
        throw new Error('当前浏览器不支持 AudioWorklet，请使用新版 Chrome/Edge');
      }

      // Step 1: Request microphone — omit sampleRate so the browser uses device default
      const mediaStartedAt = performance.now();
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      console.info(`[AudioRecorder] getUserMedia=${(performance.now() - mediaStartedAt).toFixed(1)}ms`);

      if (!mountedRef.current) {
        stream.getTracks().forEach(track => track.stop());
        startingRef.current = false;
        setIsStarting(false);
        return;
      }
      mediaStreamRef.current = stream;

      // Step 2: Resume AudioContext if suspended (required for some browsers)
      const contextStartedAt = performance.now();
      let audioContext = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
      if (audioContext.state === 'suspended') {
        try {
          await audioContext.resume();
        } catch (_e) { /* resume may fail silently */ }
      }
      audioContextRef.current = audioContext;
      console.info(
        `[AudioRecorder] AudioContext=${(performance.now() - contextStartedAt).toFixed(1)}ms ` +
        `sampleRate=${audioContext.sampleRate}`,
      );

      if (!audioContext.audioWorklet) {
        throw new Error('当前浏览器不支持 AudioWorklet，请使用新版 Chrome/Edge');
      }

      const source = audioContext.createMediaStreamSource(stream);
      sourceNodeRef.current = source;

      // Step 3: Analyser for volume monitoring
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 256;
      source.connect(analyser);
      analyserRef.current = analyser;

      const dataArray = new Uint8Array(analyser.frequencyBinCount);
      intervalRef.current = setInterval(() => {
        if (!mountedRef.current || !analyserRef.current) return;
        analyser.getByteFrequencyData(dataArray);
        const average = dataArray.reduce((a, b) => a + b) / dataArray.length;
        setVolume(average);
      }, 100);

      // Step 4: Load AudioWorklet processor
      const workletPath = '/audio-worklet/pcm-processor.js';
      const workletStartedAt = performance.now();
      await audioContext.audioWorklet.addModule(workletPath);
      console.info(`[AudioRecorder] addModule=${(performance.now() - workletStartedAt).toFixed(1)}ms`);

      if (!mountedRef.current) {
        await cleanupRecordingResources(false);
        startingRef.current = false;
        setIsStarting(false);
        return;
      }

      // Step 5: Create AudioWorkletNode
      const workletNode = new AudioWorkletNode(audioContext, 'pcm-processor');
      workletNodeRef.current = workletNode;
      recordingActiveRef.current = true;

      workletNode.port.onmessage = (event) => {
        if (!mountedRef.current || !recordingActiveRef.current || !workletNodeRef.current) return;
        if (!(event.data instanceof ArrayBuffer)) return;
        const buffer = event.data as ArrayBuffer;
        onAudioData(arrayBufferToBase64(buffer));
      };

      // Step 6: Mute output to prevent echo
      const gainNode = audioContext.createGain();
      gainNode.gain.value = 0;
      gainNodeRef.current = gainNode;

      source.connect(workletNode);
      workletNode.connect(gainNode);
      gainNode.connect(audioContext.destination);

      startingRef.current = false;
      setIsStarting(false);
      if (mountedRef.current) {
        onRecordingChange(true);
      }
      console.info(`[AudioRecorder] worklet_ready=${(performance.now() - totalStartedAt).toFixed(1)}ms`);

      // Step 7: 本地VAD只提供UI事件，后台初始化不阻塞录音。
      const vadStartedAt = performance.now();
      void (async () => {
        try {
          const vadInstance = await window.vad.MicVAD.new({
            getStream: async () => stream,
            onnxWASMBasePath: 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.22.0/dist/',
            baseAssetPath: 'https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.29/dist/',
            onSpeechStart: () => {
              if (mountedRef.current && recordingActiveRef.current) onSpeechStart?.();
            },
            onSpeechEnd: () => {
              if (mountedRef.current && recordingActiveRef.current) onSpeechEnd?.();
            },
          });
          console.info(`[AudioRecorder] MicVAD.new=${(performance.now() - vadStartedAt).toFixed(1)}ms`);
          if (!mountedRef.current || !recordingActiveRef.current || workletNodeRef.current !== workletNode) {
            vadInstance.destroy?.();
            return;
          }
          vadRef.current = vadInstance;
          const vadStartAt = performance.now();
          await vadInstance.start();
          console.info(`[AudioRecorder] MicVAD.start=${(performance.now() - vadStartAt).toFixed(1)}ms`);
          if (!recordingActiveRef.current || workletNodeRef.current !== workletNode) {
            vadInstance.pause();
            vadInstance.destroy?.();
            if (vadRef.current === vadInstance) vadRef.current = null;
          }
        } catch (e) {
          console.warn('[AudioRecorder] VAD unavailable, server VAD remains active:', e);
        }
      })();

    } catch (error) {
      startingRef.current = false;
      setIsStarting(false);
      await cleanupRecordingResources(mountedRef.current);

      if (!mountedRef.current) return;

      if (isPermissionDeniedError(error)) {
        const message = '麦克风权限被拒绝，请在浏览器地址栏左侧或设置中允许使用麦克风，然后刷新页面重试';
        onPermissionDenied?.(message);
        return;
      }

      const message = error instanceof Error ? error.message : '无法访问麦克风，请检查权限设置';
      onPermissionDenied?.(message);
    }
  };

  const stopRecording = async () => {
    if (stoppingRef.current) return;
    stoppingRef.current = true;
    startingRef.current = false;
    if (mountedRef.current) {
      setIsStarting(false);
      setIsStopping(true);
    }
    try {
      await cleanupRecordingResources(mountedRef.current);
      if (mountedRef.current) {
        if (isRecording) {
          onRecordingChange(false);
        }
        onRecordingStopped?.();
      }
    } finally {
      stoppingRef.current = false;
      if (mountedRef.current) {
        setIsStopping(false);
      }
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      void stopRecording();
    };
  }, []);

  useEffect(() => {
    if ((disabled || !isRecording) && recordingActiveRef.current && !stoppingRef.current) {
      stoppingRef.current = true;
      setIsStopping(true);
      void cleanupRecordingResources(mountedRef.current).then(() => {
        if (mountedRef.current) {
          if (isRecording) {
            onRecordingChange(false);
          }
          onRecordingStopped?.();
        }
      }).finally(() => {
        stoppingRef.current = false;
        if (mountedRef.current) setIsStopping(false);
      });
    }
  }, [disabled, isRecording, onRecordingChange, onRecordingStopped, cleanupRecordingResources]);

  const toggleRecording = async () => {
    if (isStarting || isStopping || (disabled && !isRecording)) return;
    if (isRecording) {
      await stopRecording();
    } else {
      await startRecording();
    }
  };

  return (
    <div className="relative flex items-center justify-center">
      {isRecording && (
        <div
          className="absolute rounded-full border border-primary-500/50 pointer-events-none transition-all duration-75"
          style={{
            width: `${100 + (volume / 255) * 100}%`,
            height: `${100 + (volume / 255) * 100}%`,
            opacity: Math.max(0, 1 - (volume / 255) * 1.5),
          }}
        />
      )}

      <button
        onClick={toggleRecording}
        disabled={isStarting || isStopping || (disabled && !isRecording)}
        className={`
          relative z-10 w-16 h-16 rounded-full flex items-center justify-center
          transition-all duration-300 shadow-xl
          ${isStarting || isStopping || (disabled && !isRecording) ? 'opacity-50 cursor-not-allowed shadow-none' : ''}
          ${isRecording
            ? 'bg-primary-500 hover:bg-primary-600 shadow-primary-500/40'
            : 'bg-slate-700 hover:bg-slate-600 shadow-slate-900/50'
          }
        `}
        title={isStarting ? '正在启动录音' : isStopping ? '正在停止录音' : disabled && !isRecording ? '语音识别准备中' : isRecording ? '停止录音' : '开始说话'}
      >
        {isStarting || isStopping ? (
          <Loader2 className="w-7 h-7 text-white animate-spin" />
        ) : isRecording ? (
          <Mic className="w-7 h-7 text-white" />
        ) : (
          <MicOff className="w-7 h-7 text-slate-300" />
        )}
      </button>
    </div>
  );
}
