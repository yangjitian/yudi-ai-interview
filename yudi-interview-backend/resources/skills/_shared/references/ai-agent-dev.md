# AI Agent开发面试参考

## 一、Agent基础

### Agent定义与核心架构
- Agent = LLM + Planning + Memory + Tools
- Agent Loop：感知(Perceive) -> 推理(Reason) -> 行动(Act) -> 观察(Observe)循环
- 终止条件：任务完成、达到最大步数、用户中断、错误熔断

### Agent vs 传统编程 vs 工作流
- 决策者差异：Agent由AI决策下一步，工作流由预定义路径驱动
- 适用场景判断：确定性任务用工作流，开放性任务用Agent

### Agent范式
- ReAct：Thought-Action-Observation循环，边推理边执行
- Plan-and-Execute：先规划全局步骤，再逐步执行
- Reflection：自我反思与修正（Reflexion / Self-Refine / CRITIC）

### 多Agent系统
- Orchestrator-Subagent：主从模式，编排器分配任务
- Peer-to-Peer：对等模式，Agent间平等协作
- A2A（Agent-to-Agent）通信协议

### 安全
- Prompt Injection攻击类型与防御（执行层沙箱、认知层隔离、决策层人机协同）
- Agent权限边界设计（最小权限原则）
- 敏感操作审批机制

## 二、LLM调用

### Token与上下文窗口
- Token是计费和性能的基本单位（非字符）
- 上下文窗口 = System Prompt + User Prompt + 历史 + RAG + 工具定义 + 输出
- Token预算：window >= input_tokens + max_output_tokens
- Prompt Caching：静态内容前置、动态内容后置

### 采样参数
- Temperature：控制随机性（低=确定性，高=创造性）
- Top-p（核采样）和Top-k：缩小候选词池
- Presence/Frequency Penalty：抑制重复

### Function Calling
- JSON Schema定义工具接口
- 工具粒度设计：原子操作 vs 组合操作
- 并行工具调用（Parallel Tool Calling）
- 错误处理：工具调用失败时的重试与降级策略

### 成本优化
- 输入/输出Token定价差异（2-5倍）
- 路由策略：简单问题用小模型、复杂问题用大模型
- 缓存策略：语义缓存、精确匹配缓存

## 三、MCP协议

### MCP定位
- MCP（Model Context Protocol）= AI的USB-C，统一工具接入标准
- MCP vs Function Calling vs Agent：MCP是协议标准，Function Calling是LLM能力，Agent是系统概念
- 四层关系：Function Calling（基础） -> Prompt（意图） -> MCP（连接） -> Skills（编排）

### MCP四大核心能力
- Resources：只读数据源 | Tools：可执行操作 | Prompts：模板化提示词 | Sampling：LLM推理委托

### 架构与传输
- 四层：Host（宿主） -> Client（协议客户端） -> Server（工具服务） -> Data Source
- JSON-RPC 2.0通信协议（轻量、传输无关、易调试）
- stdio：本地IPC；Streamable HTTP：远程/生产

### 生产实践
- 工具幂等性设计、退避策略与P99延迟目标
- 上下文窗口管理（大结果截断、分页）
- 安全考量（输入校验、权限控制、审计日志）

## 四、RAG检索增强

### 基本原理
- RAG = 信息检索 + LLM生成
- 离线索引（加载、清洗、分块、Embedding、存储）+ 在线检索（查询向量化、相似度搜索、上下文构建、生成）
- 核心优势：知识时效性、减少幻觉、数据安全、领域适配

### 分块与Embedding
- 固定长度 vs 语义分块 vs 递归分块
- 分块大小与语义完整性的权衡
- 通用 vs 领域微调Embedding模型选择

### 向量检索
- ANN：用5%召回率损失换100倍速度
- HNSW：<1000万向量、高召回、高内存 | IVFFLAT：1000万-1亿、内存友好
- 距离度量：余弦相似度、内积、欧几里得距离
- 混合检索：向量 + BM25 + RRF融合（生产最佳实践）

### 局限与治理
- GIGO：检索质量决定生成质量
- 上下文窗口噪声、TTFT增加、检索召回率评估

## 五、上下文工程

### 概念
- Agent = Model + Harness（模型之外的一切都是Harness）
- 模型决定上限，Harness决定下限

### 六层Harness架构
1. L1信息边界：System Prompt、约束条件、角色定义
2. L2工具系统：工具注册、Schema定义、权限控制
3. L3执行编排：Agent Loop、条件分支、并行执行
4. L4记忆与状态：短期记忆、长期记忆、状态持久化
5. L5评估与可观测：质量评估、链路追踪、指标监控
6. L6约束与恢复：错误处理、重试策略、降级方案

### Token预算与设计模式
- 40%上下文利用率阈值（超过后质量急剧下降）
- 上下文压缩：摘要、裁剪、遗忘
- 渐进式披露：L1元数据常驻 + L2正文按需 + L3资源隔离
- Lost in the Middle问题与应对

## 面试高频追问

### Agent基础
- Agent Loop中如何防止无限循环？
- ReAct和Plan-and-Execute各自的优缺点和适用场景？
- 多Agent系统中如何保证一致性？

### LLM调用
- Token超出上下文窗口时怎么处理？
- Function Calling和直接在Prompt里写工具有什么区别？
- 如何降低LLM调用的P99延迟？

### MCP协议
- MCP和直接用Function Calling有什么区别？
- stdio和Streamable HTTP分别适合什么场景？
- 如何设计一个生产级的MCP Server？

### RAG
- 分块策略如何选择？过大或过小分别有什么问题？
- 向量检索和关键词检索各自的优缺点？为什么需要混合检索？
- RAG系统线上出现幻觉，排查思路是什么？

### 上下文工程
- Agent上下文超出窗口时如何处理？
- AGENTS.md / SKILL.md的设计原则是什么？
- Harness各层级中哪一层对Agent质量影响最大？为什么？
