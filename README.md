# 印象好感度系统插件

为机器人添加动态印象和好感度记忆系统。

## 核心功能

### 智能触发机制
- **印象构建**: 基于时间间隔触发（每X分钟），避免频繁调用LLM节省token
- **好感度更新**: 基于时间触发，确保不会错过情感变化

### 权重筛选系统
- 使用LLM评估每条消息的价值和权重
- 自动过滤低价值消息（问候、客套话等）
- 只将高价值消息用于印象构建，提高准确性和效率

### 增量消息处理
- 使用 `UserMessageState` 表跟踪消息状态
- 自动去重，避免重复处理相同消息
- 限制上下文条目数量，进一步节省token

### 向量化存储
- 使用嵌入模型生成文本向量
- 支持向量相似度搜索

## 快速开始

### 配置插件

编辑 `config.toml`:

```toml
[plugin]
enabled = true

# LLM提供商配置（独立于主程序）
[llm_provider]
provider_type = "openai"
api_key = "your_api_key"
model_id = "gpt-4"

# 嵌入模型配置（必须配置）
[embedding_provider]
provider_type = "openai"
api_key = "your_api_key"
model_id = "text-embedding-ada-002"

# 好感度增幅配置
[affection_increment]
friendly_increment = 2.0    # 友善评论增幅
neutral_increment = 0.5     # 中性评论增幅
negative_increment = -3.0   # 差劲评论减幅

# 印象构建触发条件
[impression]
interval_minutes = 10        # 每10分钟触发一次印象构建
max_context_entries = 10     # 每次最多处理10条消息

# 好感度更新触发条件
[affection]
time_minutes = 15            # 每15分钟触发好感度更新

# 权重筛选配置
[weight_filter]
filter_mode = "selective"    # 仅使用高权重消息
high_weight_threshold = 70.0 # 高权重阈值

[features]
auto_update = true
enable_commands = true
enable_tools = true
```

### 支持的模型

#### LLM模型
- 任何兼容OpenAI格式的文本模型

#### 嵌入模型
- 任何兼容OpenAI格式的嵌入模型 

**重要提示**:
- 嵌入模型和维度一旦配置好请不要随意更换！
- 更换嵌入模型会导致所有向量数据失效，数据库需要重建

## 数据库结构

### user_impressions 表
```sql
CREATE TABLE user_impressions (
    user_id TEXT NOT NULL,
    impression_text TEXT NOT NULL,    -- 自然语言印象描述
    affection_score REAL NOT NULL,    -- 好感度分数(0-100)
    affection_level TEXT NOT NULL,    -- 好感度等级
    impression_vector TEXT NOT NULL,  -- JSON格式存储的向量
    context_vector TEXT,              -- JSON格式存储的上下文向量
    context TEXT NOT NULL,            -- 印象产生的上下文
    message_count INTEGER DEFAULT 1,  -- 累计消息数
    created_time DATETIME,
    updated_time DATETIME,
    last_update_time DATETIME
);
```

### user_message_state 表（增量处理）
```sql
CREATE TABLE user_message_state (
    user_id TEXT PRIMARY KEY,
    last_message_id TEXT,
    last_message_time DATETIME,
    impression_update_count INTEGER DEFAULT 0,
    affection_update_count INTEGER DEFAULT 0,
    total_messages BIGINT DEFAULT 0,       -- 总消息数
    processed_messages BIGINT DEFAULT 0    -- 已处理消息数
);
```

### user_messages 表（权重筛选）
```sql
CREATE TABLE user_messages (
    user_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    message_content TEXT NOT NULL,       -- 消息内容
    context TEXT NOT NULL,               -- 上下文信息
    weight_score REAL DEFAULT 0.0,      -- 权重分数(0-100)
    weight_level TEXT DEFAULT "low",     -- 权重等级(high/medium/low)
    is_filtered INTEGER DEFAULT 0,      -- 是否被过滤
    timestamp DATETIME,                  -- 时间戳
    UNIQUE(user_id, message_id)          -- 复合唯一索引
);
```

### impression_message_records 表（去重记录）
```sql
CREATE TABLE impression_message_records (
    user_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    impression_id TEXT,                   -- 对应的印象记录ID
    processed_time DATETIME,              -- 处理时间
    UNIQUE(user_id, message_id)           -- 复合唯一索引
);
```

## 好感度系统

### 默认值
好感度从50开始（中性）

### 累加规则
- 友善评论: +2.0
- 中性评论: +0.5
- 差劲评论: -3.0

### 等级映射
- 90-100: 非常好
- 80-89: 很好
- 70-79: 较好
- 50-69: 一般
- 30-49: 较差
- 10-29: 很差
- 0-9: 非常差

## 组件说明

### Tool组件
- `get_user_impression` - 获取用户印象和好感度数据
- `search_impressions` - 根据关键词搜索相关印象

### Action组件
- `update_user_impression` - 智能更新用户印象和好感度
  - 自动判断触发条件
  - 分离印象构建和好感度更新
  - 增量处理消息

### Command组件
- `/impression view <user_id>` - 查看用户印象
- `/impression set <user_id> <score>` - 手动设置好感度
- `/impression list` - 列出所有用户印象
- `/impression weight <user_id>` - 查看用户权重筛选统计

## 工作流程

### 消息处理流程
```
收到消息
  -> 更新UserMessageState
  -> 检查印象构建触发（消息数量）
  -> 检查好感度更新触发（时间间隔）
  -> 调用相应更新逻辑
```

### 印象构建触发
```python
if total_messages >= message_threshold:
    await build_impression()
```

### 好感度更新触发
```python
if (now - last_affection_time).minutes >= time_minutes:
    await update_affection()
```

## 配置详解

### 触发条件配置
```toml
[triggers.impression]
message_threshold = 10  # 印象构建阈值（条消息）

[triggers.affection]
time_minutes = 5  # 好感度更新间隔（分钟）
```

### 模型提供商配置
```toml
# OpenAI格式
[embedding_provider]
provider_type = "openai"
api_key = "sk-..."
model_id = "text-embedding-ada-002"

# 自定义格式
[embedding_provider]
provider_type = "custom"
api_key = "your_key"
model_id = "your_model"
api_endpoint = "https://your-api.com/v1/embeddings"
```

### 权重筛选配置
智能筛选高价值消息，避免将普通对话（打招呼等）用于印象构建。

```toml
[weight_filter]

# 权重筛选模式
# 可选值:
#   disabled: 不启用权重筛选（使用所有消息）
#   selective: 启用权重筛选，只使用高权重消息
#   balanced: 平衡模式，使用高权重和中权重消息
filter_mode = "selective"

# 权重阈值（0-100）
high_weight_threshold = 70.0  # 高权重消息的最小分数阈值
medium_weight_threshold = 40.0 # 中权重消息的最小分数阈值

# 权重评估提示词模板（可选）
weight_evaluation_prompt = """
你是一个消息权重评估助手。请根据消息内容的价值和信息量，为每条消息评估权重分数。

权重分级标准：
- 高权重 (70-100): 包含重要个人信息、情感表达、独特观点、深度话题等
- 中权重 (40-69): 有一定信息量，但不是特别重要
- 低权重 (0-39): 简单的问候、客套话、无实质内容的互动

回复要求：
1. 只返回JSON格式
2. 不要包含任何解释或其他内容

JSON格式：
{{
    "weight_score": 权重分数(0-100的浮点数),
    "weight_level": "权重等级(high/medium/low)",
    "reason": "评估原因"
}}

用户消息: {message}
上下文: {context}
"""
```

**权重筛选的优势**：
- 真正减少无效上下文：只将高价值消息用于印象构建
- 提高印象准确性：避免被普通对话影响判断
- 节省LLM token：减少提示词长度，提高效率
- 可自定义标准：根据实际需求调整权重评估标准

### 提示词模板配置
可以自定义印象分析和好感度评估的提示词模板，支持占位符替换。

```toml
[prompts]
# 印象分析提示词模板
# 支持占位符：{history_context} {message} {context}
impression_template = """
你是一个印象分析助手。请根据用户的消息生成简洁的印象描述。

要求：
- 印象描述要简洁明了，20-40字
- 保持与历史印象的一致性
- 关注用户的性格特点、行为习惯、情感倾向

请以JSON格式返回：
{{
    "impression": "印象描述",
    "reason": "形成这个印象的原因"
}}

{history_context}

用户消息: {message}
上下文: {context}
"""

# 好感度评估提示词模板
# 支持占位符：{message} {context}
affection_template = """
你是一个情感分析师。请评估用户消息的情感倾向，并给出好感度影响建议。

回复要求：
1. 只返回JSON格式，不要包含其他内容
2. 评估标准：
   - friendly: 友善的评论（赞美、鼓励、感谢等）
   - neutral: 中性的评论（客观陈述、信息性消息等）
   - negative: 差劲的评论（批评、讽刺、攻击等）

JSON格式：
{{
    "type": "评论类型（friendly/neutral/negative）",
    "reason": "评估原因"
}}

用户消息: {message}
上下文: {context}
"""
```

**注意**：
- 如果不配置提示词模板，系统将使用默认提示词
- 提示词模板中必须使用双花括号 `{{` `}}` 来转义JSON格式部分
- 印象分析模板支持三个占位符：{history_context}、{message}、{context}
- 好感度评估模板支持两个占位符：{message}、{context}
- 自定义提示词时请确保返回正确的JSON格式，否则会导致解析失败

## 优化亮点

1. **时间触发机制** - 印象构建按时间间隔触发，避免频繁LLM调用
2. **避免错过** - 好感度更新基于时间触发，及时捕捉情感变化
3. **权重筛选** - 智能过滤低价值消息，提高印象准确性和效率
4. **自动去重** - 避免重复处理相同消息，节省token
5. **上下文限制** - 每次触发限制条目数量，进一步控制token消耗
6. **增量处理** - 只处理未获取的消息，提高效率
7. **独立配置** - 不依赖主程序模型，完全独立
8. **可定制化** - 支持自定义提示词模板和权重评估标准

## 故障排除

### 插件未加载
```
错误：嵌入模型未配置！插件需要嵌入模型才能运行
```
**解决**: 配置 `embedding_provider.api_key` 和 `model_id`

### LLM调用失败
```
LLM API Key未配置
```
**解决**: 配置 `llm_provider.api_key` 和 `model_id`

### 向量维度错误
不同模型返回不同维度（1536/3072），系统会自动适配

## 许可证

MIT License