from app.infrastructure.redis.client import get_redis


RESUME_ANALYZE_STREAM_KEY = "resume:analyze:stream"
RESUME_ANALYZE_GROUP_NAME = "analyze-group"
RESUME_ANALYZE_CONSUMER_PREFIX = "analyze-consumer-"

KB_VECTORIZE_STREAM_KEY = "knowledgebase:vectorize:stream"
KB_VECTORIZE_GROUP_NAME = "vectorize-group"
KB_VECTORIZE_CONSUMER_PREFIX = "vectorize-consumer-"

INTERVIEW_EVALUATE_STREAM_KEY = "interview:evaluate:stream"
INTERVIEW_EVALUATE_GROUP_NAME = "evaluate-group"
INTERVIEW_EVALUATE_CONSUMER_PREFIX = "evaluate-consumer-"

VOICE_EVALUATE_STREAM_KEY = "voice:evaluate:stream"
VOICE_EVALUATE_GROUP_NAME = "voice-evaluate-group"
VOICE_EVALUATE_CONSUMER_PREFIX = "voice-evaluate-consumer-"

MAX_RETRY_COUNT = 3
BATCH_SIZE = 10
POLL_INTERVAL_MS = 1000
STREAM_MAX_LEN = 1000

FIELD_RESUME_ID = "resumeId"
FIELD_CONTENT = "content"
FIELD_RETRY_COUNT = "retryCount"
FIELD_KB_ID = "kbId"
FIELD_SESSION_ID = "sessionId"
FIELD_VOICE_SESSION_ID = "voiceSessionId"
FIELD_ENQUEUED_AT_NS = "enqueuedAtNs"
