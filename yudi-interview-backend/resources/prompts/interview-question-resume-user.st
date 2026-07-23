# Input Data
请生成共{question_count}个针对项目经历的面试主问题。
每个主问题必须额外生成{follow_up_count}个追问。

## 面试方向
{skill_name}（{skill_description}）

## 难度级别
{difficulty_description}

## 候选人简历
---简历内容开始---
{resume_text}
---简历内容结束---

## 已考知识点（避免重复）
{historical_section}

## 输出要求
- questions数组必须包含恰好{question_count}个元素，不多不少
- 每个主问题必须包含topicSummary字段：10字以内的知识点摘要，格式示例：
  - "Redis缓存策略设计"
  - "MySQL索引优化"
  - "Kafka异步消息解耦"
