"""
权重评估服务 - 评估消息权重，筛选高价值消息
"""

from typing import Dict, Any, Tuple, Optional
from datetime import datetime
from collections import defaultdict

from ..clients import LLMClient
from .database_service import DatabaseService
from src.common.logger import get_logger

logger = get_logger("impression_affection_weight")


class WeightService:
    """权重评估服务"""

    def __init__(self, llm_client: LLMClient, config: Dict[str, Any]):
        self.llm_client = llm_client
        self.config = config
        self.weight_config = config.get("weight_filter", {})
        self.prompts_config = config.get("prompts", {})

        # 阈值配置
        self.high_threshold = self.weight_config.get("high_weight_threshold", 70.0)
        self.medium_threshold = self.weight_config.get("medium_weight_threshold", 40.0)
        self.filter_mode = self.weight_config.get("filter_mode", "selective")
        
        # 自定义权重模型配置
        self.use_custom_weight_model = self.weight_config.get("use_custom_weight_model", False)
        self.weight_llm_client = None
        
        # 内存存储 - 按用户存储消息权重记录
        self.message_weights = defaultdict(list)  # {user_id: [(message_id, score, level, timestamp), ...]}
        
        # 数据库服务
        self.db_service = DatabaseService(config)
        
        # 初始化权重评估客户端
        self._init_weight_llm_client()

    def _init_weight_llm_client(self):
        """初始化权重评估专用LLM客户端"""
        if not self.use_custom_weight_model:
            return
        
        # 避免重复初始化
        if self.weight_llm_client is not None:
            return
        
        try:
            weight_model_config = {
                "provider_type": self.weight_config.get("weight_model_provider", "openai"),
                "api_key": self.weight_config.get("weight_model_api_key", ""),
                "base_url": self.weight_config.get("weight_model_base_url", "https://api.openai.com/v1"),
                "model_id": self.weight_config.get("weight_model_id", "gpt-3.5-turbo")
            }
            
            self.weight_llm_client = LLMClient(weight_model_config)
            logger.info(f"权重评估模型已初始化: {weight_model_config['model_id']}")
            
        except Exception as e:
            logger.error(f"权重评估模型初始化失败: {str(e)}")
            self.weight_llm_client = None

    async def evaluate_message(self, user_id: str, message_id: str, message: str, context: str = "") -> Tuple[bool, float, str]:
        """
        评估消息权重

        Args:
            user_id: 用户ID
            message_id: 消息ID
            message: 消息内容
            context: 上下文

        Returns:
            (是否成功, 权重分数, 权重等级)
        """
        try:
            # 检查消息是否已存在
            user_messages = self.message_weights.get(user_id, [])
            for msg_record in user_messages:
                if msg_record[0] == message_id:  # message_id matches
                    return True, msg_record[1], msg_record[2]  # score, level

            # 生成提示词（包含上下文）
            prompt = self._build_weight_prompt(message, context)

            # 选择LLM客户端进行评估
            if self.use_custom_weight_model and self.weight_llm_client:
                # 使用自定义权重模型
                success, content = await self.weight_llm_client.generate_weight_evaluation(prompt)
            else:
                # 使用默认LLM客户端
                success, content = await self.llm_client.generate_weight_evaluation(prompt)

            if not success:
                # 评估失败，使用默认权重
                return self._save_default_weight(user_id, message_id, message, context)

            # 解析结果
            result = self._parse_weight_response(content)

            if not result:
                # 解析失败，使用默认权重
                return self._save_default_weight(user_id, message_id, message, context)

            weight_score = float(result.get("weight_score", 0.0))
            weight_level = result.get("weight_level", "low")

            # 保存到内存
            self._save_weight(user_id, message_id, message, context, weight_score, weight_level)

            return True, weight_score, weight_level

        except Exception as e:
            return False, 0.0, f"评估权重失败: {str(e)}"

    def _build_weight_prompt(self, message: str, context: str) -> str:
        """构建权重评估提示词"""
        template = self.prompts_config.get("weight_evaluation_prompt", "").strip()

        if template:
            return template.format(message=message, context=context)

        # 默认提示词 - 使用键值对格式
        return f"""基于消息内容和上下文对话，评估消息权重（0-100）。权重评估标准：高权重(70-100): 包含重要个人信息、兴趣爱好、价值观、情感表达、深度思考、独特观点、生活经历分享；中权重(40-69): 一般日常对话、简单提问、客观陈述、基础信息交流；低权重(0-39): 简单问候、客套话、无实质内容的互动、表情符号。特别注意：结合上下文判断，分享个人喜好、询问对方偏好、表达个人观点都应该给予较高权重。只返回键值对格式：WEIGHT_SCORE: 分数;WEIGHT_LEVEL: high/medium/low;REASON: 评估原因;当前消息: {message};历史上下文: {context}"""

    def _parse_weight_response(self, content: str) -> Optional[Dict[str, Any]]:
        """解析权重评估响应 - 支持键值对和JSON两种格式"""
        import logging
        import re
        import json
        
        logger = logging.getLogger("impression_affection_system")
        
        # 清理内容
        content = content.strip()
        
        # 如果内容太短
        if len(content) < 10:
            logger.error(f"LLM响应太短: {repr(content)}")
            return None
        
        # 方法1: 尝试提取键值对格式
        score_match = re.search(r'WEIGHT_SCORE:\s*([\d.]+)', content, re.IGNORECASE)
        level_match = re.search(r'WEIGHT_LEVEL:\s*(\w+)', content, re.IGNORECASE)
        reason_match = re.search(r'REASON:\s*(.+?)(?:\n|;消息:|$)', content, re.IGNORECASE)
        
        result = {}
        if score_match:
            try:
                result["weight_score"] = float(score_match.group(1))
            except:
                result["weight_score"] = 0.0
        if level_match:
            result["weight_level"] = level_match.group(1).strip().lower()
        if reason_match:
            result["reason"] = reason_match.group(1).strip()
        
        if result and "weight_score" in result:
            logger.debug(f"提取到键值对格式权重数据: {result}")
            return result
        
        # 方法2: 尝试解析JSON格式（作为后备）
        try:
            # 查找JSON部分（从第一个{到最后一个}）
            start = content.find('{')
            end = content.rfind('}')
            
            if start >= 0 and end >= 0 and end > start:
                json_str = content[start:end+1]
                json_result = json.loads(json_str)
                
                if isinstance(json_result, dict) and "weight_score" in json_result:
                    logger.debug(f"提取到JSON格式权重数据: {json_result}")
                    return json_result
        except json.JSONDecodeError as e:
            logger.debug(f"JSON解析失败: {e}")
        
        logger.error(f"无法提取权重数据: {repr(content)}")
        return None

    def _save_weight(self, user_id: str, message_id: str, message: str, context: str, weight_score: float, weight_level: str):
        """保存权重评估结果到内存"""
        self.message_weights[user_id].append((
            message_id, 
            weight_score, 
            weight_level, 
            datetime.now(),
            message[:100],  # 保存消息内容的前100字符
            context[:100]   # 保存上下文的前100字符
        ))
        
        # 限制每个用户最多保存100条记录
        if len(self.message_weights[user_id]) > 100:
            self.message_weights[user_id] = self.message_weights[user_id][-100:]

    def _save_default_weight(self, user_id: str, message_id: str, message: str, context: str) -> Tuple[bool, float, str]:
        """保存默认权重"""
        # 简单规则：消息长度大于20字认为是中权重
        if len(message) > 20:
            weight_score = 50.0
            weight_level = "medium"
        else:
            weight_score = 20.0
            weight_level = "low"

        self._save_weight(user_id, message_id, message, context, weight_score, weight_level)
        return True, weight_score, weight_level

    def get_filtered_messages(self, user_id: str, limit: int = 10) -> Tuple[str, list]:
        """
        获取筛选后的消息用于印象构建（包含上下文）

        Args:
            user_id: 用户ID
            limit: 最大消息数

        Returns:
            (上下文字符串, 消息ID列表)
        """
        if self.filter_mode == "disabled":
            return "", []

        # 首先尝试从数据库获取历史消息
        db_messages = self._get_historical_messages(user_id, limit)
        
        # 合并内存中的消息权重记录
        user_messages = self.message_weights.get(user_id, [])
        
        # 根据筛选模式过滤消息
        filtered_messages = []
        
        # 处理数据库历史消息（默认给予中等权重）
        for db_msg in db_messages:
            filtered_messages.append({
                "message_id": db_msg["message_id"],
                "weight_score": 50.0,  # 历史消息默认中等权重
                "weight_level": "medium",
                "timestamp": db_msg["datetime"],
                "content": db_msg["content"],
                "source": "database"
            })
        
        # 处理内存中的消息权重记录
        for msg_record in user_messages:
            message_id, weight_score, weight_level, timestamp, message_content, context = msg_record
            
            if self.filter_mode == "selective":
                if weight_score >= self.high_threshold:
                    filtered_messages.append({
                        "message_id": message_id,
                        "weight_score": weight_score,
                        "weight_level": weight_level,
                        "timestamp": timestamp,
                        "content": message_content,
                        "source": "memory",
                        "context": context
                    })
            elif self.filter_mode == "balanced":
                if weight_score >= self.medium_threshold:
                    filtered_messages.append({
                        "message_id": message_id,
                        "weight_score": weight_score,
                        "weight_level": weight_level,
                        "timestamp": timestamp,
                        "content": message_content,
                        "source": "memory",
                        "context": context
                    })
            else:  # 默认包含所有消息
                filtered_messages.append({
                    "message_id": message_id,
                    "weight_score": weight_score,
                    "weight_level": weight_level,
                    "timestamp": timestamp,
                    "content": message_content,
                    "source": "memory",
                    "context": context
                })

        # 按时间倒序排列，取前limit条
        filtered_messages.sort(key=lambda x: x["timestamp"], reverse=True)
        filtered_messages = filtered_messages[:limit]

        if not filtered_messages:
            return "", []

        # 构建上下文（包含历史对话信息）
        contexts = []
        message_ids = []
        
        # 首先添加历史对话摘要
        if db_messages:
            contexts.append("历史对话背景:")
            for db_msg in db_messages[:5]:  # 显示最近5条历史消息作为背景
                timestamp = db_msg["datetime"]
                content = db_msg["content"][:80]  # 限制长度
                if isinstance(timestamp, datetime):
                    time_str = timestamp.strftime('%m-%d %H:%M')
                else:
                    time_str = str(timestamp)
                contexts.append(f"[{time_str}] {content}...")

        # 然后添加当前筛选的消息
        contexts.append("\n当前对话:")
        for msg in filtered_messages:
            timestamp = msg["timestamp"]
            content = msg["content"]
            weight_score = msg["weight_score"]
            weight_level = msg["weight_level"]
            source = msg["source"]
            
            if isinstance(timestamp, datetime):
                time_str = timestamp.strftime('%m-%d %H:%M')
            else:
                time_str = str(timestamp)
            
            context_str = f"[{time_str}] {content}"
            if weight_score > 0:
                context_str += f" (权重: {weight_score:.1f}, 等级: {weight_level})"
            if source == "database":
                context_str += " [历史]"
            
            contexts.append(context_str)
            message_ids.append(msg["message_id"])

        result = "\n".join(contexts)

        return result, message_ids

    def _get_historical_messages(self, user_id: str, limit: int = 10) -> list:
        """
        从数据库获取历史消息
        
        Args:
            user_id: 用户ID
            limit: 最大消息数
            
        Returns:
            历史消息列表
        """
        if not self.db_service or not self.db_service.is_connected():
            return []
        
        try:
            # 从配置获取参数
            history_config = self.config.get("history", {})
            max_messages = history_config.get("max_messages", 20)
            hours_back = history_config.get("hours_back", 72)
            min_length = history_config.get("min_message_length", 5)
            
            # 转换小时数为天数（数据库服务需要天数参数）
            days_back = max(1, hours_back // 24)  # 至少1天
            
            # 使用配置的参数获取历史消息
            history_messages = self.db_service.get_user_chat_history(
                user_id=user_id,
                limit=min(limit, max_messages),  # 不超过配置的最大值
                days_back=days_back
            )
            
            # 过滤掉太短的消息
            filtered_messages = [
                msg for msg in history_messages 
                if msg["content"] and len(msg["content"].strip()) >= min_length
            ]
            
            return filtered_messages[:limit]
            
        except Exception as e:
            # 记录错误但不影响主流程
            print(f"获取历史消息失败: {str(e)}")
            return []

    def get_user_chat_summary(self, user_id: str) -> Dict[str, Any]:
        """
        获取用户聊天摘要
        
        Args:
            user_id: 用户ID
            
        Returns:
            聊天摘要信息
        """
        if not self.db_service or not self.db_service.is_connected():
            return {"error": "数据库未连接"}
        
        try:
            return self.db_service.get_user_chat_summary(user_id, days_back=7)
        except Exception as e:
            return {"error": f"获取摘要失败: {str(e)}"}

    def search_user_messages(self, user_id: str, keyword: str, limit: int = 5) -> list:
        """
        搜索用户消息
        
        Args:
            user_id: 用户ID
            keyword: 搜索关键词
            limit: 最大结果数
            
        Returns:
            匹配的消息列表
        """
        if not self.db_service or not self.db_service.is_connected():
            return []
        
        try:
            return self.db_service.search_user_messages(user_id, keyword, limit)
        except Exception as e:
            print(f"搜索用户消息失败: {str(e)}")
            return []

    def get_recent_interactions(self, user_id: str, hours_back: int = 24) -> list:
        """
        获取用户最近的互动记录
        
        Args:
            user_id: 用户ID
            hours_back: 回溯小时数
            
        Returns:
            最近的互动记录
        """
        if not self.db_service or not self.db_service.is_connected():
            return []
        
        try:
            return self.db_service.get_recent_interactions(user_id, hours_back)
        except Exception as e:
            print(f"获取最近互动失败: {str(e)}")
            return []

    def get_historical_context_for_weight(self, user_id: str) -> str:
        """
        获取用户历史上下文（专门用于权重评估）
        
        Args:
            user_id: 用户ID
            
        Returns:
            历史上下文字符串
        """
        try:
            # 从配置获取参数
            history_config = self.config.get("history", {})
            max_messages = history_config.get("max_messages", 20)
            hours_back = history_config.get("hours_back", 72)
            min_length = history_config.get("min_message_length", 5)
            
            # 转换小时数为天数
            days_back = max(1, hours_back // 24)
            
            # 获取历史消息
            history_messages = self.db_service.get_user_chat_history(
                user_id=user_id,
                limit=max_messages,
                days_back=days_back
            )
            
            # 过滤掉太短的消息
            filtered_messages = [
                msg for msg in history_messages 
                if msg["content"] and len(msg["content"].strip()) >= min_length
            ]
            
            if not filtered_messages:
                return ""
            
            # 构建上下文
            contexts = []
            contexts.append("历史对话:")
            
            # 使用配置中的最大消息数
            for msg in filtered_messages:
                timestamp = msg["datetime"]
                content = msg["content"]  # 不限制长度，让配置控制
                if isinstance(timestamp, datetime):
                    time_str = timestamp.strftime('%m-%d %H:%M')
                else:
                    time_str = str(timestamp)
                contexts.append(f"[{time_str}] {content}")
            
            return "\n".join(contexts)
            
        except Exception as e:
            logger.error(f"获取权重评估历史上下文失败: {str(e)}")
            return ""