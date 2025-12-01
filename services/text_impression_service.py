"""
文本印象服务 - 基于LLM的纯文本多维度印象管理
"""

from typing import Dict, Any, Tuple, Optional, List
from datetime import datetime

from ..models import UserImpression
from ..clients import LLMClient
from ..utils.constants import AFFECTION_LEVELS
from .database_service import DatabaseService


class TextImpressionService:
    """文本印象服务 - 基于LLM分析用户消息并更新多维度印象"""

    def __init__(self, llm_client: LLMClient, config: Dict[str, Any]):
        self.llm_client = llm_client
        self.config = config
        self.prompts_config = config.get("prompts", {})
        self.db_service = DatabaseService(config)

    async def build_impression(self, user_id: str, message: str, history_context: str = "") -> Tuple[bool, str]:
        """
        构建用户印象 - 基于LLM分析的多维度印象
        
        Args:
            user_id: 用户ID
            message: 当前消息
            history_context: 历史上下文
            
        Returns:
            (是否成功, 印象描述)
        """
        try:
            # 获取增强的历史上下文
            enhanced_context = await self._get_enhanced_context(user_id, history_context)
            
            # 生成提示词
            prompt = self._build_prompt(enhanced_context, message)
            
            # 调用LLM生成印象
            success, content = await self.llm_client.generate_impression_analysis(prompt)

            if not success:
                return False, f"LLM调用失败: {content}"

            # 解析结果
            impression_data = self._parse_impression_response(content)

            if not impression_data:
                return False, f"解析失败: {content}"

            # 保存到数据库
            await self._save_impression(user_id, impression_data, message, enhanced_context)

            return True, impression_data.get("impression", "印象构建成功")

        except Exception as e:
            return False, f"构建印象失败: {str(e)}"

    async def _get_enhanced_context(self, user_id: str, existing_context: str) -> str:
        """
        获取增强的历史上下文（严格验证用户身份）
        
        Args:
            user_id: 用户ID
            existing_context: 现有的上下文
            
        Returns:
            增强后的上下文
        """
        try:
            # 检查是否启用数据库功能
            db_config = self.config.get("database", {})
            if not db_config.get("enabled", True):
                return existing_context
                
            # 如果数据库服务不可用，返回原有上下文
            if not self.db_service or not self.db_service.is_connected():
                return existing_context
            
            # 从配置获取参数
            history_config = self.config.get("history", {})
            hours_back = history_config.get("hours_back", 72)
            recent_hours = history_config.get("recent_hours", 24)
            
            # 获取用户聊天摘要（严格验证用户ID）
            summary = self.db_service.get_user_chat_summary(user_id, days_back=max(1, hours_back // 24))
            
            # 获取最近的互动（严格验证用户ID）
            recent_interactions = self.db_service.get_recent_interactions(user_id, hours_back=recent_hours)
            
            # 构建增强上下文
            enhanced_parts = []
            
            # 添加用户标识
            enhanced_parts.append(f"用户 {user_id} 的印象构建上下文")
            
            # 添加聊天摘要
            if "error" not in summary and summary.get("total_messages", 0) > 0:
                summary_text = f"用户统计: {hours_back}小时内共{summary['total_messages']}条消息"
                if summary.get("active_groups"):
                    groups = [g["name"] for g in summary["active_groups"][:3]]
                    summary_text += f", 主要活跃群组: {', '.join(groups)}"
                enhanced_parts.append(summary_text)
            
            # 添加最近互动
            verified_interactions = []
            for interaction in recent_interactions[:10]:  # 取最近10条
                if interaction.get("content") and len(interaction["content"].strip()) >= 2:
                    content = interaction["content"][:150]  # 限制长度
                    hours_ago = interaction.get("hours_ago", 0)
                    verified_interactions.append(f"[{hours_ago:.1f}小时前] {content}")
            
            if verified_interactions:
                enhanced_parts.append(f"最近{recent_hours}小时内的对话记录:")
                enhanced_parts.extend(verified_interactions)
            
            # 合并原有上下文
            if existing_context:
                enhanced_parts.append("当前会话上下文:")
                enhanced_parts.append(existing_context)
            
            # 限制总长度
            enhanced_context = "\n".join(enhanced_parts)
            max_context_length = self.config.get("history", {}).get("max_messages", 20) * 100  # 估算长度
            if len(enhanced_context) > max_context_length:
                enhanced_context = enhanced_context[:max_context_length] + "..."
            
            # 添加上下文来源说明
            enhanced_context += f"\n\n基于用户 {user_id} 的历史对话记录生成"
            
            return enhanced_context
            
        except Exception as e:
            # 发生错误时返回原有上下文
            logger.warning(f"获取历史上下文失败: {str(e)}")
            return existing_context

    def _build_prompt(self, history_context: str, message: str) -> str:
        """构建印象分析提示词"""
        template = self.prompts_config.get("impression_template", "").strip()

        # 从配置获取长度限制
        max_history_chars = self.config.get("max_history_chars", 2000)
        max_message_chars = self.config.get("max_message_chars", 500)

        if template:
            return template.format(
                history_context=history_context[:max_history_chars],
                message=message[:max_message_chars],
                context=""
            )

        # 默认提示词 - 使用配置的长度限制
        limited_history = history_context[:max_history_chars] if len(history_context) > max_history_chars else history_context
        limited_message = message[:max_message_chars] if len(message) > max_message_chars else message
        
        return f"请基于用户的聊天记录生成印象描述，用自然语言描述这个人的性格特点、兴趣爱好、交流方式等，长度50-100字。要求语言自然流畅，像朋友介绍这个人一样。如果信息不足，可以适当推测并用'似乎'、'看起来'等词。历史对话: {limited_history} 当前消息: {limited_message}"

    def _parse_impression_response(self, content: str) -> Dict[str, str]:
        """解析LLM响应 - 处理自然语言印象"""
        import logging
        
        logger = logging.getLogger("impression_affection_system")
        
        try:
            # 清理内容
            content = content.strip()
            
            # 如果内容太短，返回空
            if len(content) < 10:
                logger.warning("印象响应内容过短")
                return {}
            
            # 直接返回自然语言印象
            result = {
                "impression": content
            }
            
            logger.debug(f"解析到自然印象: {content[:50]}...")
            return result
            
        except Exception as e:
            logger.error(f"解析印象响应异常: {str(e)}")
            return {}

    async def _save_impression(self, user_id: str, impression_data: Dict[str, str], message: str, context: str):
        """保存印象数据到数据库"""
        try:
            # 获取或创建用户记录
            impression, created = UserImpression.get_or_create(
                user_id=user_id,
                defaults={
                    "affection_score": 50.0,
                    "affection_level": "一般",
                    "message_count": 0
                }
            )

            # 保存自然语言印象到主印象字段
            natural_impression = impression_data.get("impression", "")
            if natural_impression:
                # 将自然印象保存到主印象字段
                impression.interests_hobbies = natural_impression  # 复用现有字段存储自然印象
                
                # 清空其他维度字段，避免混淆
                impression.personality_traits = ""
                impression.communication_style = ""
                impression.emotional_tendencies = ""
                impression.behavioral_patterns = ""
                impression.values_attitudes = ""
                impression.relationship_preferences = ""
                impression.growth_development = ""

            # 更新统计信息
            if created:
                impression.message_count = 1
            else:
                impression.message_count += 1

            impression.last_interaction = datetime.now()
            impression.update_timestamps()
            impression.save()

        except Exception as e:
            print(f"保存印象失败: {str(e)}")

    def get_impression(self, user_id: str) -> Optional[UserImpression]:
        """获取用户印象"""
        try:
            return UserImpression.select().where(
                UserImpression.user_id == user_id
            ).first()
        except Exception as e:
            print(f"获取印象失败: {str(e)}")
            return None

    async def update_dimension(self, user_id: str, dimension: str, content: str) -> Tuple[bool, str]:
        """更新特定维度的内容"""
        try:
            impression = self.get_impression(user_id)
            if not impression:
                return False, "用户不存在"

            impression.set_dimension(dimension, content)
            return True, f"{dimension}已更新: {content}"
        except Exception as e:
            return False, f"更新维度失败: {str(e)}"

    async def get_dimension(self, user_id: str, dimension: str) -> str:
        """获取特定维度的内容"""
        try:
            impression = self.get_impression(user_id)
            if not impression:
                return "用户不存在"
            
            return impression.get_dimension(dimension)
        except Exception as e:
            return f"获取维度失败: {str(e)}"

    def get_impression_summary(self, user_id: str) -> str:
        """获取印象摘要"""
        try:
            impression = self.get_impression(user_id)
            if not impression:
                return "暂无用户印象数据"
            
            # 返回自然语言印象
            natural_impression = impression.interests_hobbies  # 从复用的字段获取
            if natural_impression:
                return natural_impression
            else:
                return "用户印象正在构建中..."
                
        except Exception as e:
            return f"获取摘要失败: {str(e)}"