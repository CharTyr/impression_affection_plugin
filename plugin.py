"""
印象和好感度系统插件
"""

from typing import List, Tuple, Type, Dict, Any, Optional
import os
import asyncio

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    ComponentInfo,
    ConfigField,
    BaseEventHandler,
    EventType,
    CustomEventHandlerResult
)
from src.common.logger import get_logger

# 导入模型
from .models import db, UserImpression, UserMessageState, ImpressionMessageRecord
from .models.database import DB_PATH

# 导入客户端
from .clients import LLMClient

# 导入服务
from .services import (
    AffectionService,
    WeightService,
    TextImpressionService,
    MessageService
)

# 导入组件
from .components import (
    GetUserImpressionTool,
    SearchImpressionsTool,
    ViewImpressionCommand,
    SetAffectionCommand,
    ListImpressionsCommand
)


logger = get_logger("impression_affection_system")


class ImpressionUpdateHandler(BaseEventHandler):
    """自动更新用户印象和好感度的事件处理器（异步执行）"""

    event_type = EventType.AFTER_LLM
    handler_name = "update_impression_handler"
    handler_description = "每次LLM回复后更新用户印象和好感度"
    intercept_message = False  # 不拦截消息，允许正常回复

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.affection_service = None
        self.weight_service = None
        self.message_service = None
        self.llm_client = None
        self.text_impression_service = None
        self._services_initialized = False

    async def execute(self, message) -> tuple:
        """执行事件处理器 - 异步启动印象更新任务"""
        try:
            # 确保服务已初始化
            self._ensure_services_initialized()
            
            # 异步启动印象更新，不阻塞主流程
            asyncio.create_task(self._async_update_impression(message))
            return True, True, "印象更新任务已启动", None, None
                
        except Exception as e:
            logger.error(f"印象更新执行失败: {str(e)}")
            return True, True, f"印象更新执行失败: {str(e)}", None, None

    async def _async_update_impression(self, event_data):
        """异步更新印象和好感度"""
        try:
            # 确保服务已初始化
            self._ensure_services_initialized()
            
            # 执行印象更新逻辑
            result = await self.handle(event_data)
            
        except Exception as e:
            logger.error(f"印象更新失败: {str(e)}")
            # 异步执行中的错误不影响主流程

    def _ensure_services_initialized(self):
        """确保服务已初始化（只初始化一次）"""
        if self._services_initialized:
            return
        
        self._init_services()
        self._services_initialized = True

    def _init_services(self):
        """初始化服务"""
        if not self.llm_client:
            llm_config = self.plugin_config.get("llm_provider", {})
            self.llm_client = LLMClient(llm_config)

        if not self.affection_service:
            self.affection_service = AffectionService(self.llm_client, self.plugin_config)

        if not self.weight_service:
            self.weight_service = WeightService(self.llm_client, self.plugin_config)

        if not self.text_impression_service:
            self.text_impression_service = TextImpressionService(self.llm_client, self.plugin_config)

        if not self.message_service:
            self.message_service = MessageService(self.plugin_config)

    async def handle(self, event_data) -> CustomEventHandlerResult:
        """处理事件：每次LLM回复后自动更新印象和好感度"""
        try:
            # 确保服务已初始化
            self._ensure_services_initialized()
            
            logger.debug(f"收到AFTER_LLM事件，事件数据类型: {type(event_data)}")

            # 获取消息对象 - 兼容不同的事件数据格式
            message = None
            user_id = ""
            
            # 尝试不同的消息获取方式
            if hasattr(event_data, 'message_base_info'):
                message = event_data
                user_id = str(message.message_base_info.get('user_id', ''))
            elif hasattr(event_data, 'user_id'):
                user_id = str(event_data.user_id)
                message = event_data
            elif hasattr(event_data, 'plain_text'):
                user_id = str(getattr(event_data, 'user_id', ''))
                message = event_data
            else:
                # 尝试从事件数据中提取消息
                if hasattr(event_data, '__dict__'):
                    for attr_name in ['message', 'msg', 'data']:
                        if hasattr(event_data, attr_name):
                            potential_msg = getattr(event_data, attr_name)
                            if hasattr(potential_msg, 'user_id'):
                                user_id = str(potential_msg.user_id)
                                message = potential_msg
                                break
                
                if not user_id:
                    logger.error(f"无法从事件数据中提取用户ID: {event_data}")
                    return CustomEventHandlerResult(message="无法从事件数据中提取用户ID")

            if not user_id:
                logger.error(f"用户ID为空")
                return CustomEventHandlerResult(message="无法获取用户ID")

            # 获取消息内容
            message_content = self._extract_message_content(message)
            if not message_content:
                logger.warning(f"用户 {user_id} 的消息内容为空")
                return CustomEventHandlerResult(message="消息内容为空")

            # 生成消息ID
            import time
            message_id = str(int(time.time() * 1000))

            # 检查消息是否已处理
            if self.message_service.is_message_processed(user_id, message_id):
                logger.debug(f"用户 {user_id} 的消息 {message_id} 已处理，跳过")
                return CustomEventHandlerResult(message="消息已处理，跳过")

            logger.info(f"开始处理用户 {user_id} 的消息: {message_content[:50]}...")

            # 获取权重评估所需的历史上下文
            history_context = self.weight_service.get_historical_context_for_weight(user_id)
            logger.debug(f"获取到权重评估历史上下文，长度: {len(history_context)}")

            # 评估消息权重（传递历史上下文）
            logger.debug(f"开始评估消息权重 - 用户: {user_id}, 消息: {message_content[:50]}...")
            weight_success, weight_score, weight_level = await self.weight_service.evaluate_message(
                user_id, message_id, message_content, history_context
            )

            # 获取筛选后的历史消息（用于印象构建）
            history_context, processed_ids = self.weight_service.get_filtered_messages(user_id)
            logger.debug(f"获取到印象构建历史上下文，长度: {len(history_context)}")

            if not weight_success:
                logger.warning(f"权重评估失败: {weight_level}")
            else:
                logger.info(f"权重评估成功 - 分数: {weight_score}, 等级: {weight_level}")

            # 根据权重等级决定是否更新印象
            impression_updated = False
            should_update_impression = False
            
            if weight_success:
                # 检查权重等级是否满足更新条件
                filter_mode = self.weight_service.filter_mode
                high_threshold = self.weight_service.high_threshold
                medium_threshold = self.weight_service.medium_threshold
                
                if filter_mode == "disabled":
                    should_update_impression = True
                elif filter_mode == "selective":
                    should_update_impression = weight_score >= high_threshold
                elif filter_mode == "balanced":
                    should_update_impression = weight_score >= medium_threshold
                
                logger.info(f"权重筛选检查 - 模式: {filter_mode}, 分数: {weight_score}, 阈值: {high_threshold}/{medium_threshold}, 是否更新印象: {should_update_impression}")
            else:
                logger.warning(f"权重评估失败，跳过印象更新")
                should_update_impression = False

            # 更新印象
            if should_update_impression:
                try:
                    logger.debug(f"开始构建印象 - 用户: {user_id}, 消息: {message_content[:50]}...")
                    success, impression_result = await self.text_impression_service.build_impression(
                        user_id, message_content, history_context
                    )
                    if success:
                        impression_updated = True
                        logger.info(f"印象更新成功: {impression_result[:50]}...")
                    else:
                        logger.warning(f"印象更新失败: {impression_result}")
                except Exception as e:
                    logger.error(f"印象更新异常: {str(e)}")
            else:
                logger.info(f"权重等级不满足印象更新条件 (分数: {weight_score}, 等级: {weight_level})，跳过印象更新")

            # 更新好感度
            affection_updated = False
            try:
                success, affection_result = await self.affection_service.update_affection(
                    user_id, message_content
                )
                if success:
                    affection_updated = True
                    logger.info(f"好感度更新成功: {affection_result}")
                else:
                    logger.warning(f"好感度更新失败: {affection_result}")
            except Exception as e:
                logger.error(f"好感度更新异常: {str(e)}")

            # 更新消息状态
            self.message_service.update_message_state(
                user_id, message_id, impression_updated, affection_updated
            )

            # 记录已处理的消息
            for msg_id in processed_ids:
                self.message_service.record_processed_message(user_id, msg_id)

            self.message_service.record_processed_message(user_id, message_id)

            return CustomEventHandlerResult(message="印象和好感度更新完成")

        except Exception as e:
            logger.error(f"处理事件失败: {str(e)}")
            return CustomEventHandlerResult(message=f"异常: {str(e)}")

    def _extract_message_content(self, message) -> str:
        """提取消息内容"""
        message_content = ""

        if hasattr(message, 'plain_text') and message.plain_text:
            message_content = str(message.plain_text)
        elif hasattr(message, 'message_segments') and message.message_segments:
            message_content = " ".join([
                str(seg.data) for seg in message.message_segments
                if hasattr(seg, 'data')
            ])

        return message_content.strip()


@register_plugin
class ImpressionAffectionPlugin(BasePlugin):
    """印象和好感度系统插件"""

    # 插件基本信息
    plugin_name = "impression_affection_plugin"
    enable_plugin = True
    dependencies = []
    python_dependencies = ["peewee", "openai", "httpx"]
    config_file_name = "config.toml"

    # 配置模式 - 详细的配置定义
    config_schema = {
        # =============================================================================
        # 基础配置
        # =============================================================================
        "plugin": {
            "enabled": ConfigField(
                type=bool,
                default=False,
                description="是否启用插件"
            ),
            "config_version": ConfigField(
                type=str,
                default="2.1.1",
                description="配置文件版本"
            )
        },

        # =============================================================================
        # LLM 配置
        # =============================================================================
        "llm_provider": {
            "provider_type": ConfigField(
                type=str,
                default="openai",
                description="印象构建/好感度更新LLM提供商 (openai/custom)"
            ),
            "api_key": ConfigField(
                type=str,
                default="",
                description="API密钥"
            ),
            "base_url": ConfigField(
                type=str,
                default="https://api.openai.com/v1",
                description="API基础URL"
            ),
            "model_id": ConfigField(
                type=str,
                default="gpt-3.5-turbo",
                description="模型名称"
            )
        },

        # =============================================================================
        # 数据库配置
        # =============================================================================
        "database": {
            "enabled": ConfigField(
                type=bool,
                default=True,
                description="启用数据库连接"
            ),
            "main_db_path": ConfigField(
                type=str,
                default="",
                description="数据库路径 (留空使用默认)"
            )
        },

        # =============================================================================
        # 历史消息配置
        # =============================================================================
        "history": {
            "max_messages": ConfigField(
                type=int,
                default=20,
                description="最大历史消息数 (10-50)"
            ),
            "hours_back": ConfigField(
                type=int,
                default=72,
                description="回溯小时数 (24-168)"
            ),
            "min_message_length": ConfigField(
                type=int,
                default=5,
                description="最小消息长度"
            ),
            "recent_hours": ConfigField(
                type=int,
                default=24,
                description="最近互动回溯小时数 (6-48)"
            )
        },

        # =============================================================================
        # 权重筛选配置
        # =============================================================================
        "weight_filter": {
            "filter_mode": ConfigField(
                type=str,
                default="selective",
                description="筛选模式: disabled(不筛选)/selective(仅高权重)/balanced(仅高/中权重)"
            ),
            "high_weight_threshold": ConfigField(
                type=float,
                default=70.0,
                description="高权重阈值 (60.0-80.0)"
            ),
            "medium_weight_threshold": ConfigField(
                type=float,
                default=40.0,
                description="中权重阈值 (30.0-50.0)"
            ),
            "use_custom_weight_model": ConfigField(
                type=bool,
                default=False,
                description="是否启用自定义权重判断模型"
            ),
            "weight_model_provider": ConfigField(
                type=str,
                default="openai",
                description="权重判断模型提供商"
            ),
            "weight_model_api_key": ConfigField(
                type=str,
                default="",
                description="权重判断模型API密钥"
            ),
            "weight_model_base_url": ConfigField(
                type=str,
                default="https://api.openai.com/v1",
                description="权重判断模型API地址"
            ),
            "weight_model_id": ConfigField(
                type=str,
                default="gpt-3.5-turbo",
                description="权重判断模型ID"
            ),
            "weight_evaluation_prompt": ConfigField(
                type=str,
                default="基于消息内容和上下文对话，评估消息权重（0-100）。权重评估标准：高权重(70-100): 包含重要个人信息、兴趣爱好、价值观、情感表达、深度思考、独特观点、生活经历分享；中权重(40-69): 一般日常对话、简单提问、客观陈述、基础信息交流；低权重(0-39): 简单问候、客套话、无实质内容的互动、表情符号。特别注意：结合上下文判断，分享个人喜好、询问对方偏好、表达个人观点都应该给予较高权重。只返回键值对格式：WEIGHT_SCORE: 分数;WEIGHT_LEVEL: high/medium/low;REASON: 评估原因;当前消息: {message};历史上下文: {context}",
                description="权重评估提示词模板"
            ),
            "max_history_chars": ConfigField(
                type=int,
                default=2000,
                description="历史上下文最大字符数"
            ),
            "max_message_chars": ConfigField(
                type=int,
                default=500,
                description="消息最大字符数"
            )
        },

        # =============================================================================
        # 好感度配置
        # =============================================================================
        "affection_increment": {
            "friendly_increment": ConfigField(
                type=float,
                default=2.0,
                description="友善消息增幅 (1.0-5.0)"
            ),
            "neutral_increment": ConfigField(
                type=float,
                default=0.5,
                description="中性消息增幅 (0.1-1.0)"
            ),
            "negative_increment": ConfigField(
                type=float,
                default=-3.0,
                description="负面消息增幅 (-5.0到-1.0)"
            )
        },

        # =============================================================================
        # 提示词模板
        # =============================================================================
        "prompts": {
            "impression_template": ConfigField(
                type=str,
                default="请基于用户的聊天记录生成印象描述，用自然语言描述这个人的性格特点、兴趣爱好、交流方式等，长度50-100字。要求语言自然流畅，像朋友介绍这个人一样。如果信息不足，可以适当推测并用'似乎'、'看起来'等词。历史对话: {history_context} 当前消息: {message}",
                description="印象分析提示词模板"
            ),
            "affection_template": ConfigField(
                type=str,
                default="评估用户消息情感倾向（friendly/neutral/negative）。只返回键值对格式：TYPE: friendly/neutral/negative;REASON: 评估原因;消息: {message}",
                description="好感度评估提示词模板"
            )
        },

        # =============================================================================
        # 功能开关
        # =============================================================================
        "features": {
            "auto_update": ConfigField(
                type=bool,
                default=True,
                description="自动更新印象和好感度"
            ),
            "enable_commands": ConfigField(
                type=bool,
                default=True,
                description="启用管理命令"
            ),
            "enable_tools": ConfigField(
                type=bool,
                default=True,
                description="启用工具组件"
            )
        }
    }

    def __init__(self, plugin_dir: str = None):
        super().__init__(plugin_dir)
        self.db_initialized = False

    def init_db(self):
        """初始化数据库"""
        if not self.db_initialized:
            try:
                db.connect()
                
                # 确保导入所有模型
                from .models import (
                    UserImpression,
                    UserMessageState, 
                    ImpressionMessageRecord
                )
                
                # 创建所有表
                db.create_tables([
                    UserImpression,
                    UserMessageState,
                    ImpressionMessageRecord
                ], safe=True)
                
                self.db_initialized = True
                logger.info(f"数据库初始化成功: {DB_PATH}")
                
                # 验证表是否创建成功
                tables = db.get_tables()
                logger.info(f"已创建的表: {tables}")
                
            except Exception as e:
                logger.error(f"数据库初始化失败: {str(e)}")
                raise e

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件组件列表"""
        self.init_db()

        components = []

        # 添加事件处理器
        components.append((ImpressionUpdateHandler.get_handler_info(), ImpressionUpdateHandler))

        # 根据配置添加组件
        features_config = self.get_config("features", {})

        if features_config.get("enable_tools", True):
            # 添加工具组件
            components.extend([
                (GetUserImpressionTool.get_tool_info(), GetUserImpressionTool),
                (SearchImpressionsTool.get_tool_info(), SearchImpressionsTool)
            ])

        if features_config.get("enable_commands", True):
            # 添加命令组件
            components.extend([
                (ViewImpressionCommand.get_command_info(), ViewImpressionCommand),
                (SetAffectionCommand.get_command_info(), SetAffectionCommand),
                (ListImpressionsCommand.get_command_info(), ListImpressionsCommand)
            ])

        return components