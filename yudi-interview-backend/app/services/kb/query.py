import logging
import asyncio
import os
from pathlib import Path
from typing import AsyncGenerator

from langchain_core.messages import HumanMessage, SystemMessage
from app.infrastructure.ai.embedding_client import EmbeddingClient
from app.infrastructure.ai.provider_registry import get_plain_chat_client
from app.repositories.kb_repository import KbRepository


log = logging.getLogger(__name__)

# 与 Java 版本一致：未检索到相关信息时的固定回复
_NO_RESULT_RESPONSE = "抱歉，在选定的知识库中未检索到相关信息。请换一个更具体的关键词或补充上下文后再试。"

# Prompt 模板目录（相对于项目根目录）
_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "resources" / "prompts"

# 流式探测窗口大小（与 Java 的 STREAM_PROBE_CHARS=120 一致）
_STREAM_PROBE_CHARS = 120

# Query Rewrite 历史最大字符数（与 Java 的 MAX_REWRITE_HISTORY_CHAR=200 一致）
_MAX_REWRITE_HISTORY_CHAR = 200

# 动态 topK 配置（与 Java 的 KnowledgeBaseQueryProperties.Search 一致）
_SHORT_QUERY_LENGTH = int(os.getenv("RAG_SHORT_QUERY_LENGTH", "4"))
_TOP_K_SHORT = int(os.getenv("RAG_TOP_K_SHORT", "20"))
_TOP_K_MEDIUM = int(os.getenv("RAG_TOP_K_MEDIUM", "12"))
_TOP_K_LONG = int(os.getenv("RAG_TOP_K_LONG", "8"))
_MIN_SCORE_SHORT = float(os.getenv("RAG_MIN_SCORE_SHORT", "0.25"))
_MIN_SCORE_DEFAULT = float(os.getenv("RAG_MIN_SCORE_DEFAULT", "0.28"))

# Query Rewrite 配置
_REWRITE_ENABLED = os.getenv("RAG_REWRITE_ENABLED", "true").lower() == "true"


def _load_prompt_template(filename: str) -> str:
    """加载 prompt 模板文件内容。"""
    path = _PROMPTS_DIR / filename
    if not path.exists():
        log.warning("Prompt 模板文件不存在: %s，使用内置默认模板", path)
        return ""
    return path.read_text(encoding="utf-8")


class KnowledgeBaseQueryService:
    def __init__(self, kb_repo: KbRepository):
        self.kb_repo = kb_repo
        # 加载 prompt 模板（启动时一次性读取，与 Java 版本一致）
        self._system_prompt_template = _load_prompt_template(
            "knowledgebase-query-system.st"
        )
        self._user_prompt_template = _load_prompt_template(
            "knowledgebase-query-user.st"
        )
        self._rewrite_prompt_template = _load_prompt_template(
            "knowledgebase-query-rewrite.st"
        )

    def _build_messages(
        self, context: str, question: str, history: list[dict] | None = None
    ) -> list[SystemMessage | HumanMessage]:
        """
        构建发送给 LLM 的消息列表。
        使用 knowledgebase-query-system.st 和 knowledgebase-query-user.st 模板，
        与 Java 版本的 buildSystemPrompt / buildUserPrompt 逻辑一致。
        """
        # System prompt：直接使用模板内容（模板中无变量占位符）
        system_content = self._system_prompt_template.strip()
        if not system_content:
            system_content = (
                "你是一位专业的知识库问答助手，擅长基于检索增强生成（RAG）技术为用户提供准确、详尽的答案。"
                "只使用知识库中检索到的相关信息，不编造或推测任何内容。"
            )

        # User prompt：将 {context} 和 {question} 替换进模板
        user_content = self._user_prompt_template
        if user_content:
            user_content = user_content.replace("{context}", context)
            user_content = user_content.replace("{question}", question)
        else:
            user_content = (
                f"请根据以下知识库内容回答用户的问题。\n\n"
                f"## 检索到的相关文档\n"
                f"---文档内容开始---\n{context}\n---文档内容结束---\n\n"
                f"## 用户问题\n{question}\n\n"
                f"请开始回答："
            )

        messages = [SystemMessage(content=system_content)]

        # 注入历史上下文（与 Java 的 sanitizeHistory + effectiveHistory 逻辑一致）
        if history:
            for msg in history:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user":
                    messages.append(HumanMessage(content=f"用户: {content}"))
                elif role == "assistant":
                    messages.append(HumanMessage(content=f"助手: {content}"))

        messages.append(HumanMessage(content=user_content))
        return messages

    def _resolve_search_params(self, question: str) -> tuple[int, float]:
        """
        根据问题长度动态调整 topK 和相似度阈值。
        与 Java 的 resolveSearchParams 逻辑一致。
        """
        compact_length = len(question.replace(" ", "").replace("\n", ""))
        if compact_length <= _SHORT_QUERY_LENGTH:
            return _TOP_K_SHORT, _MIN_SCORE_SHORT
        if compact_length <= 12:
            return _TOP_K_MEDIUM, _MIN_SCORE_DEFAULT
        return _TOP_K_LONG, _MIN_SCORE_DEFAULT

    async def _rewrite_question(
        self, question: str, history: list[dict] | None = None
    ) -> str:
        """
        Query Rewrite：将用户问题改写成更适合检索的形式。
        与 Java 的 rewriteQuestion 逻辑一致。
        """
        if not _REWRITE_ENABLED or not question.strip():
            return question

        if not self._rewrite_prompt_template:
            return question

        try:
            # 格式化历史消息（与 Java 的 formatHistoryForRewrite 逻辑一致）
            history_text = ""
            if history:
                history_parts = []
                for msg in history[-10:]:  # 最多取最近 10 条
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if len(content) > _MAX_REWRITE_HISTORY_CHAR:
                        content = content[:_MAX_REWRITE_HISTORY_CHAR] + "..."
                    if role == "user":
                        history_parts.append(f"用户: {content}")
                    elif role == "assistant":
                        history_parts.append(f"助手: {content}")
                if history_parts:
                    history_text = "\n".join(history_parts)

            # 构建 rewrite prompt
            rewrite_prompt = self._rewrite_prompt_template
            rewrite_prompt = rewrite_prompt.replace("{question}", question)
            if history_text:
                rewrite_prompt = rewrite_prompt.replace("{history}", f"\n\n对话历史：\n{history_text}")
            else:
                rewrite_prompt = rewrite_prompt.replace("{history}", "")

            # 调用 LLM 进行 rewrite
            chat = await get_plain_chat_client(None)
            response = await chat.ainvoke([HumanMessage(content=rewrite_prompt)])
            rewritten = response.content if hasattr(response, "content") else str(response)

            if rewritten and rewritten.strip():
                log.info("Query rewrite: origin='%s', rewritten='%s'", question, rewritten.strip())
                return rewritten.strip()
            return question
        except Exception as e:
            log.warning("Query rewrite 失败，使用原问题继续检索: %s", e)
            return question

    async def _retrieve_relevant_docs(
        self,
        query_text: str,
        kb_ids: list[int],
        history: list[dict] | None = None,
    ) -> list[dict]:
        """
        检索相关文档，支持 Query Rewrite 和动态 topK。
        与 Java 的 retrieveRelevantDocs 逻辑一致。
        """
        original_query = query_text.strip()
        if not original_query:
            return []

        # 动态参数
        top_k, min_score = self._resolve_search_params(original_query)

        rewrite_task = asyncio.create_task(
            self._rewrite_question(original_query, history)
        )
        original_search_task = asyncio.create_task(
            self._search_chunks(original_query, kb_ids, top_k, min_score)
        )

        rewritten = await rewrite_task
        original_error: Exception | None = None
        try:
            original_chunks = await original_search_task
        except Exception as error:
            original_error = error
            original_chunks = []

        # 等价于 Java LinkedHashSet：保持改写优先，同时去除相同候选。
        candidates = list(dict.fromkeys((rewritten, original_query)))
        if candidates[0] != original_query:
            rewritten_chunks = await self._search_chunks(
                candidates[0], kb_ids, top_k, min_score
            )
            log.info(
                "检索候选 query='%s'，命中 %d 条",
                candidates[0],
                len(rewritten_chunks),
            )
            if rewritten_chunks:
                return rewritten_chunks

        if original_error is not None:
            raise original_error
        log.info(
            "检索候选 query='%s'，命中 %d 条",
            original_query,
            len(original_chunks),
        )
        if original_chunks:
            return original_chunks

        return []

    def _normalize_answer(self, answer: str | None) -> str:
        """
        规范化回答，与 Java 的 normalizeAnswer 逻辑一致。
        """
        if not answer or not answer.strip():
            return _NO_RESULT_RESPONSE

        normalized = answer.strip()
        no_result_patterns = [
            "没有找到相关信息",
            "未检索到相关信息",
            "信息不足",
            "超出知识库范围",
            "无法根据提供内容回答",
        ]
        for pattern in no_result_patterns:
            if pattern in normalized:
                return _NO_RESULT_RESPONSE

        return normalized

    async def query(
        self,
        query_text: str,
        knowledge_base_ids: list[int],
        top_k: int = 5,
        history: list[dict] | None = None,
    ) -> dict:
        if not knowledge_base_ids:
            return {"answer": _NO_RESULT_RESPONSE, "chunks": []}

        # 与 Java 一致：先验证知识库并记录本次提问，再执行检索。
        await self.kb_repo.increment_question_counts(knowledge_base_ids)

        chunks = await self._retrieve_relevant_docs(
            query_text, knowledge_base_ids, history
        )

        if not chunks:
            log.info(
                "RAG 查询无结果: kb_ids=%s, question='%s'",
                knowledge_base_ids,
                query_text,
            )
            return {"answer": _NO_RESULT_RESPONSE, "chunks": []}

        context = "\n\n---\n\n".join(c["content"] for c in chunks)
        log.info(
            "RAG 查询命中 %d 个片段: kb_ids=%s, question='%s'",
            len(chunks),
            knowledge_base_ids,
            query_text,
        )

        messages = self._build_messages(context, query_text, history)

        try:
            chat = await get_plain_chat_client(None)
            response = await chat.ainvoke(messages)
            answer = response.content if hasattr(response, "content") else str(response)
            answer = self._normalize_answer(answer)
        except Exception as e:
            log.error("RAG 查询失败: %s", e)
            answer = "抱歉，AI 服务暂时不可用，请稍后重试。"

        return {
            "answer": answer,
            "chunks": [
                {
                    "content": c["content"],
                    "score": c["score"],
                    "source": c.get("source", ""),
                }
                for c in chunks
            ],
        }

    async def query_stream(
        self,
        query_text: str,
        knowledge_base_ids: list[int],
        top_k: int = 5,
        history: list[dict] | None = None,
    ) -> AsyncGenerator[str, None]:
        if not knowledge_base_ids:
            yield _NO_RESULT_RESPONSE
            return

        # 与 Java 一致：先验证知识库并记录本次提问，再执行检索。
        await self.kb_repo.increment_question_counts(knowledge_base_ids)

        chunks = await self._retrieve_relevant_docs(
            query_text, knowledge_base_ids, history
        )

        if not chunks:
            log.info(
                "RAG 流式查询无结果: kb_ids=%s, question='%s'",
                knowledge_base_ids,
                query_text,
            )
            yield _NO_RESULT_RESPONSE
            return

        context = "\n\n---\n\n".join(c["content"] for c in chunks)
        log.info(
            "RAG 流式查询命中 %d 个片段: kb_ids=%s, question='%s'",
            len(chunks),
            knowledge_base_ids,
            query_text,
        )

        messages = self._build_messages(context, query_text, history)

        try:
            chat = await get_plain_chat_client(None)
            # 流式探测窗口归一化（与 Java 的 normalizeStreamOutput 逻辑一致）
            probe_buffer = ""
            passthrough = False

            async for chunk in chat.astream(messages):
                text = chunk.content if hasattr(chunk, "content") else str(chunk)

                if not passthrough:
                    probe_buffer += text
                    if len(probe_buffer) >= _STREAM_PROBE_CHARS:
                        # 探测窗口已满，释放缓冲区并透传后续内容
                        passthrough = True
                        yield probe_buffer
                        probe_buffer = ""
                    else:
                        # 检查是否命中"无结果"模式
                        no_result_patterns = [
                            "没有找到相关信息",
                            "未检索到相关信息",
                            "信息不足",
                            "超出知识库范围",
                            "无法根据提供内容回答",
                        ]
                        hit_pattern = next(
                            (p for p in no_result_patterns if p in probe_buffer),
                            None,
                        )
                        if hit_pattern:
                            log.info(
                                "RAG 流式回答命中无结果模式，按业务规则主动结束上游流: "
                                "pattern='%s', probe_chars=%d",
                                hit_pattern,
                                len(probe_buffer),
                            )
                            yield _NO_RESULT_RESPONSE
                            return
                else:
                    yield text

            # 流结束，如果还未透传则规范化缓冲区内容
            if not passthrough and probe_buffer:
                yield self._normalize_answer(probe_buffer)
        except Exception as e:
            log.error("RAG 流式查询失败: %s", e)
            yield "抱歉，AI 服务暂时不可用，请稍后重试。"

    async def _search_chunks(
        self,
        query_text: str,
        kb_ids: list[int],
        top_k: int,
        similarity_threshold: float = 0.5,
    ) -> list[dict]:
        embedding_client = EmbeddingClient()
        query_vector = await embedding_client.embed_text(query_text)
        chunks = await self.kb_repo.search_chunks_by_vector(
            query_vector,
            kb_ids,
            top_k,
            similarity_threshold,
        )

        log.debug(
            "向量检索完成: kb_ids=%s, query='%s', top_k=%d, threshold=%.2f, 返回 %d 条结果",
            kb_ids,
            query_text,
            top_k,
            similarity_threshold,
            len(chunks),
        )
        return chunks
