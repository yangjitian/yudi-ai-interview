-- =============================================================================
-- 向量检索索引（HNSW + cosine）
--
-- 背景
--   sql/public.sql 里 vector_store 的索引定义为：
--     CREATE INDEX "spring_ai_vector_index" ON "public"."vector_store"
--       ("embedding" "public"."vector_cosine_ops");
--   这条语句缺少 USING hnsw / ivfflat 子句，PostgreSQL 会按默认访问方法（btree）解析，
--   而 vector_cosine_ops 并不是 btree 的合法操作符类，对向量距离排序起不到加速作用
--   （能否成功建出来取决于实际执行情况，本仓库的 dump 无法证明线上形态）。
--
-- 前置条件
--   1. 已安装 pgvector 扩展，且版本 >= 0.5.0（HNSW 访问方法自 0.5.0 引入）。
--      检查：SELECT extversion FROM pg_extension WHERE extname = 'vector';
--   2. 距离度量必须与代码一致：检索使用 cosine_distance
--      （app/repositories/kb_repository.py::search_chunks_by_vector），
--      因此操作符类固定为 vector_cosine_ops。
--   3. 向量维度必须与代码一致：EMBEDDING_DIMENSION = 1024
--      （app/models/knowledge_base.py），对应列类型 vector(1024)。
--
-- 执行前必读
--   * 本文件需手动执行，未在任何环境运行过（作者本地也未连库执行）。
--   * 先确认现有索引的实际类型：
--       \d+ public.vector_store
--     若已存在 HNSW 或 IVFFlat 索引（只是 dump 丢了 USING 子句），不要重复创建——
--     下面的 IF NOT EXISTS 只按索引名判重，无法阻止同义索引重复建立。
--   * HNSW 建索引会全表扫描，大表请放到低峰期，并临时提高 maintenance_work_mem。
--   * 建索引期间写入会变慢，建议先备份。
--
-- 执行方式
--   psql -h <host> -p <port> -U <user> -d <db> -f sql/add_vector_index.sql
-- =============================================================================

-- 可选：加速索引构建（仅当前会话生效）
-- SET maintenance_work_mem = '512MB';

CREATE INDEX IF NOT EXISTS vector_store_embedding_hnsw_idx
  ON public.vector_store
  USING hnsw (embedding vector_cosine_ops);

-- HNSW 构建参数使用 pgvector 默认值：
--   m = 16                （每个节点的双向连接数，越大召回越好、内存与构建越慢）
--   ef_construction = 64  （构建时的候选集大小）
-- 需要调整时写成：
--   ... USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
--
-- 查询期参数（会话级，非索引定义）：
--   SET hnsw.ef_search = 40;   -- 默认 40，越大召回越好、越慢

-- 验证（执行后）：
--   EXPLAIN (ANALYZE, BUFFERS)
--   SELECT content FROM public.vector_store
--   WHERE metadata->>'kb_id' = '<kb_id>'
--   ORDER BY embedding <=> '<query_vector>'::vector
--   LIMIT 8;
-- 关注是否走 vector_store_embedding_hnsw_idx，以及是否仍需要全表扫描后再过滤
-- metadata->>'kb_id'（该过滤条件目前没有索引）。
