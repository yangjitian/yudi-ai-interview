# Input Data
请生成共{question_count}个主问题。
每个主问题都必须额外生成{follow_up_count}个追问。

## 难度级别
{difficulty_description}

## 面试方向
{skill_name}（{skill_description}）

## 问题分布要求
{allocation_table}

## 已考知识点（避免重复）
{historical_section}

## 参考题库（references）
{reference_section}

## 职位描述
---职位描述开始---
{jd_text}
---职位描述结束---

## 输出要求
- questions数组必须包含恰好{question_count}个元素，不多不少
- 每个主问题必须包含topicSummary字段：10字以内的知识点摘要，格式示例：
  - "Redis RDB/AOF持久化对比"
  - "MySQL索引失效场景"
  - "HashMap扩容机制"
  - "TCP三次握手流程"
