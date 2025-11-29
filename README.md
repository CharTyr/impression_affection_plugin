# 印象好感度系统插件

一个基于LLM的智能印象和好感度分析系统，能够持续跟踪用户的性格特征和行为模式。

## 核心功能

### 必触发机制
- **印象构建**: 每次收到消息时必定执行，持续积累用户特征
- **好感度更新**: 每次收到消息时必定执行，及时响应情感变化
- **由planner决定**: 当planner决定使用"reply"动作时，插件必定运行
- **无频率限制**: 不再依赖消息数量阈值，每次调用都会执行

### 权重筛选系统
- 使用LLM评估每条消息的价值和权重
- 自动过滤低价值消息（问候、客套话等）
- 只将高价值消息用于印象构建，提高准确性和效率
- 可配置阈值：选择性（仅高权重）或平衡性（高+中权重）

### 增量消息处理
- 使用 `UserMessageState` 表跟踪消息状态
- 自动去重，避免重复处理相同消息
- 限制上下文条目数量，进一步节省token
- 每次触发最多处理指定数量的消息

### 向量化存储
- 使用嵌入模型生成文本向量
- 支持向量相似度搜索
- 支持上下文检索和智能匹配

### 持续学习
- 每次消息都会更新印象和好感度
- 保持历史印象的一致性
- 动态跟踪用户特征变化

## 插件触发机制

### 激活类型: ALWAYS
该插件使用 ALWAYS 激活类型，这意味着：
- 插件会在planner决定使用"reply"动作时必定运行
- 不需要LLM判定触发条件
- 每次用户消息都会被处理（除非被权重筛选过滤）

### 触发频率
- **v1.2.0**: 每次收到消息时必定执行，无频率限制
- **v1.1.x及之前**: 基于消息数量阈值，需要达到一定消息数才触发

## 配置选项

### 基础配置
```toml
[plugin]
enabled = true  # 是否启用插件
```

### 权重筛选配置
```toml
[weight_filter]
enabled = true  # 是否启用权重筛选
filter_mode = "selective"  # 筛选模式: disabled(禁用)/selective(仅高权重)/balanced(高+中权重)
high_weight_threshold = 70.0  # 高权重消息阈值(0-100)
medium_weight_threshold = 40.0  # 中权重消息阈值(0-100)
```

### 印象分析配置
```toml
[impression]
max_context_entries = 30  # 每次触发时获取的上下文条目上限
```

### LLM提示词模板
```toml
[prompts]
# 自定义LLM提示词内容，支持占位符
weight_evaluation_prompt = "你是一个消息权重评估助手..."
impression_template = "你是一个印象分析助手..."
affection_template = "你是一个情感分析师..."
```

## 数据库表

### UserImpression
存储用户印象数据
- `user_id`: 用户唯一标识
- `impression_text`: 印象描述文本
- `impression_vector`: 印象向量（用于相似度搜索）
- `last_updated`: 最后更新时间

### UserAffection
存储用户好感度数据
- `user_id`: 用户唯一标识
- `affection_score`: 好感度分数
- `affection_level`: 好感度等级
- `change_reason`: 变化原因
- `last_updated`: 最后更新时间

### UserMessageState
跟踪用户消息状态
- `user_id`: 用户唯一标识
- `total_messages`: 总消息数
- `impression_update_count`: 印象更新计数
- `affection_update_count`: 好感度更新计数

### MessageRecord
存储消息记录
- `user_id`: 用户唯一标识
- `message_id`: 消息唯一标识
- `message_content`: 消息内容
- `message_vector`: 消息向量
- `weight_score`: 权重分数
- `processed`: 是否已处理

## 依赖要求

### 必需
- MaiBot 插件系统
- LLM服务 (用于分析)
- 嵌入模型服务 (用于向量存储和相似度搜索)

## 使用建议

1. **配置LLM服务**: 确保LLM服务正常工作，这是核心依赖
2. **配置嵌入模型**: 必需，插件依赖向量化存储和相似度搜索功能
3. **调整权重阈值**: 根据需要调整权重筛选的阈值
4. **监控日志**: 观察插件运行日志，确保正常执行

## 注意事项

- 插件会持续分析每条消息，可能产生一定的token消耗
- 权重筛选可以帮助减少无效消息的处理
- 建议定期清理旧的数据库记录以节省存储空间