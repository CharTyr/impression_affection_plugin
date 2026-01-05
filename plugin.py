"""
印象和好感度系统插件
"""

from dataclasses import dataclass
import json
import re
import time
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
    ActionCheckAction,
    GetUserImpressionTool,
    SearchImpressionsTool,
    ViewImpressionCommand,
    SetAffectionCommand,
    ListImpressionsCommand,
    ToggleActionCheckCommand,
    ToggleActionCheckShowResultCommand,
)


logger = get_logger("impression_affection_system")


# =============================================================================
# 动作检定（planner -> replyer）上下文传递
#
# 重要约束：
# - 不改主程序：replyer 不会读取 action_data，因此需要通过 planner 的“推理文本”写入标记行。
# - 回复展示标签不能放在 llm_response_content 中（会被主程序后处理移除），因此在发送阶段加前缀。
# =============================================================================

_ACTION_CHECK_MARKER_PREFIX = "ACTION_CHECK_JSON:"
_ACTION_CHECK_SENTINEL = "[impression_affection_plugin:action_check]"
_ACTION_CHECK_CONTEXT_TTL_SECONDS = 120.0
_ACTION_CHECK_PENDING_TAG_TTL_SECONDS = 60.0


@dataclass
class ActionCheckContext:
    stream_id: str
    interaction: str
    chance: int
    result: str  # "success" | "fail"
    created_at: float


_ACTION_CHECK_CONTEXT_BY_STREAM: Dict[str, ActionCheckContext] = {}
_ACTION_CHECK_PENDING_TAG_BY_STREAM: Dict[str, Tuple[str, float]] = {}


def _now_ts() -> float:
    return time.time()


def _clean_expired_action_check_state(stream_id: str) -> None:
    ctx = _ACTION_CHECK_CONTEXT_BY_STREAM.get(stream_id)
    if ctx and (_now_ts() - ctx.created_at) > _ACTION_CHECK_CONTEXT_TTL_SECONDS:
        _ACTION_CHECK_CONTEXT_BY_STREAM.pop(stream_id, None)
    tag_entry = _ACTION_CHECK_PENDING_TAG_BY_STREAM.get(stream_id)
    if tag_entry and (_now_ts() - tag_entry[1]) > _ACTION_CHECK_PENDING_TAG_TTL_SECONDS:
        _ACTION_CHECK_PENDING_TAG_BY_STREAM.pop(stream_id, None)


def _parse_action_check_marker(text: str) -> Optional[ActionCheckContext]:
    if not text:
        return None

    matches = re.findall(rf"{re.escape(_ACTION_CHECK_MARKER_PREFIX)}\s*(\{{[^\r\n]*\}})", text)
    if not matches:
        return None

    raw_json = matches[-1].strip()
    try:
        payload = json.loads(raw_json)
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    interaction = str(payload.get("interaction", "")).strip()
    result = str(payload.get("result", "")).strip().lower()
    chance_raw = payload.get("chance")

    try:
        chance = int(chance_raw)
    except Exception:
        return None

    if result in {"ok", "pass", "passed", "success"}:
        result = "success"
    elif result in {"fail", "failed", "failure"}:
        result = "fail"
    else:
        return None

    chance = max(0, min(100, chance))
    if not interaction:
        return None

    return ActionCheckContext(
        stream_id="",
        interaction=interaction,
        chance=chance,
        result=result,
        created_at=_now_ts(),
    )


def _strip_action_check_marker_lines(text: str) -> str:
    if not text:
        return text
    return re.sub(rf"(?m)^\s*{re.escape(_ACTION_CHECK_MARKER_PREFIX)}.*(?:\r?\n)?", "", text)


def _format_action_check_tag(chance: int, result: str) -> str:
    zh_result = "成功" if result == "success" else "失败"
    return f"[动作检定： {chance}% {zh_result}]"


class ActionCheckPlannerPromptHandler(BaseEventHandler):
    """为 planner 注入动作检定输出协议（不改主程序）"""

    event_type = EventType.ON_PLAN
    handler_name = "action_check_planner_prompt"
    handler_description = "为 planner 注入动作检定（action_check）输出规则"
    intercept_message = True
    weight = 100

    async def execute(self, message) -> tuple:
        try:
            enabled = bool(self.get_config("action_check.enabled", False))
            if not enabled or not message or not message.llm_prompt:
                return True, True, None, None, None

            if _ACTION_CHECK_SENTINEL in message.llm_prompt:
                return True, True, None, None, None

            show_roll_result = bool(self.get_config("action_check.show_roll_result", False))

            platform = str(message.message_base_info.get("platform", "") or "")
            raw_user_id = str(message.message_base_info.get("user_id", "") or "")
            from .services.message_service import MessageService

            user_id = MessageService.normalize_user_id(raw_user_id)

            affection_score = 50.0
            affection_level = "一般"
            try:
                imp = UserImpression.select().where(UserImpression.user_id == user_id).first()
                if imp and imp.affection_score is not None:
                    affection_score = float(imp.affection_score)
                    affection_level = str(imp.affection_level or affection_level)
            except Exception:
                pass

            extra_block = f"""

{_ACTION_CHECK_SENTINEL}
【动作检定（action_check）插件协议】
当你判断“用户正在尝试与麦麦进行直接动作互动”（例如：抱抱、亲亲、摸头、rua 等）时：
1) 请在同一轮规划中同时选择 reply 与 action_check（不要只选 action_check）。
2) action_check 的 JSON 需要包含字段：
   - interaction: 动作名（字符串）
   - chance: 成功率（0-100 整数，已综合基础概率/好感度/上下文）
   - result: success 或 fail（由你根据 chance 与上下文直接决定，不要让程序随机）
3) 必须在 JSON 代码块之前的推理文本末尾追加一行（仅当选择 action_check 时输出）：
   ACTION_CHECK_JSON: {{"interaction":"抱抱","chance":80,"result":"fail"}}
   - 这行必须是单行严格 JSON
   - 不要输出除这一行之外的其他 ACTION_CHECK_JSON 标记

当前用户信息（供你参考）：
- platform: {platform}
- user_id: {raw_user_id}
- affection_score: {affection_score:.1f}/100
- affection_level: {affection_level}

备注：{'机器人会在最终回复开头自动加上检定标签，你无需在回复正文里复述该标签。' if show_roll_result else '当前配置关闭了检定标签展示。'}
""".strip(
                "\n"
            )

            message.modify_llm_prompt(f"{message.llm_prompt.rstrip()}\n\n{extra_block}")
            return True, True, "动作检定 planner 协议已注入", None, message
        except Exception as e:
            logger.error(f"动作检定 planner 注入失败: {e}", exc_info=True)
            return True, True, None, None, None


class ActionCheckPostLLMHandler(BaseEventHandler):
    """为 replyer 注入检定结果块，并缓存上下文供发送阶段加标签"""

    event_type = EventType.POST_LLM
    handler_name = "action_check_post_llm"
    handler_description = "在 replyer 生成前注入动作检定结果"
    intercept_message = True
    weight = 100

    async def execute(self, message) -> tuple:
        try:
            enabled = bool(self.get_config("action_check.enabled", False))
            if not enabled or not message or not message.llm_prompt or not message.stream_id:
                return True, True, None, None, None

            stream_id = str(message.stream_id)
            _clean_expired_action_check_state(stream_id)

            parsed = _parse_action_check_marker(message.llm_prompt)
            if not parsed:
                _ACTION_CHECK_CONTEXT_BY_STREAM.pop(stream_id, None)
                return True, True, None, None, None

            parsed.stream_id = stream_id
            _ACTION_CHECK_CONTEXT_BY_STREAM[stream_id] = parsed

            cleaned_prompt = _strip_action_check_marker_lines(message.llm_prompt)
            if cleaned_prompt is None:
                cleaned_prompt = message.llm_prompt

            zh_result = "成功" if parsed.result == "success" else "失败"
            injected = f"""

{_ACTION_CHECK_SENTINEL}
【动作检定结果】
动作：{parsed.interaction}
成功率：{parsed.chance}%
结果：{zh_result}

以上信息仅供你生成回复时参考。
注意：不要在回复中输出 ACTION_CHECK_JSON 或其他内部标记。
""".strip(
                "\n"
            )

            message.modify_llm_prompt(f"{cleaned_prompt.rstrip()}\n\n{injected}")
            return True, True, "动作检定结果已注入 replyer prompt", None, message
        except Exception as e:
            logger.error(f"动作检定 POST_LLM 注入失败: {e}", exc_info=True)
            return True, True, None, None, None


class ActionCheckAfterLLMHandler(BaseEventHandler):
    """在 LLM 生成后标记待发送标签（发送阶段加前缀，避免主程序后处理移除）"""

    event_type = EventType.AFTER_LLM
    handler_name = "action_check_after_llm"
    handler_description = "动作检定：为发送阶段准备前缀标签"
    intercept_message = True
    weight = 100

    async def execute(self, message) -> tuple:
        try:
            enabled = bool(self.get_config("action_check.enabled", False))
            show_roll_result = bool(self.get_config("action_check.show_roll_result", False))
            if not enabled or not show_roll_result or not message or not message.stream_id:
                return True, True, None, None, None

            stream_id = str(message.stream_id)
            _clean_expired_action_check_state(stream_id)

            ctx = _ACTION_CHECK_CONTEXT_BY_STREAM.get(stream_id)
            if not ctx:
                return True, True, None, None, None

            tag = _format_action_check_tag(ctx.chance, ctx.result)
            _ACTION_CHECK_PENDING_TAG_BY_STREAM[stream_id] = (tag, _now_ts())
            return True, True, "动作检定标签已准备", None, None
        except Exception as e:
            logger.error(f"动作检定 AFTER_LLM 处理失败: {e}", exc_info=True)
            return True, True, None, None, None


class ActionCheckPostSendPrefixHandler(BaseEventHandler):
    """在发送前把检定标签前缀加到首条文本消息上（保证不被后处理剥离）"""

    event_type = EventType.POST_SEND_PRE_PROCESS
    handler_name = "action_check_post_send_prefix"
    handler_description = "动作检定：发送前为首条文本消息加前缀标签"
    intercept_message = True
    weight = 100

    async def execute(self, message) -> tuple:
        try:
            enabled = bool(self.get_config("action_check.enabled", False))
            show_roll_result = bool(self.get_config("action_check.show_roll_result", False))
            if not enabled or not show_roll_result or not message or not message.stream_id:
                return True, True, None, None, None

            stream_id = str(message.stream_id)
            _clean_expired_action_check_state(stream_id)

            pending = _ACTION_CHECK_PENDING_TAG_BY_STREAM.get(stream_id)
            if not pending:
                return True, True, None, None, None

            tag, _ = pending
            if not message.message_segments:
                return True, True, None, None, None

            modified_segments = None
            for seg in message.message_segments:
                if getattr(seg, "type", None) != "text":
                    continue
                if not isinstance(getattr(seg, "data", None), str):
                    continue
                text = str(seg.data)
                if text.lstrip().startswith("[动作检定："):
                    _ACTION_CHECK_PENDING_TAG_BY_STREAM.pop(stream_id, None)
                    _ACTION_CHECK_CONTEXT_BY_STREAM.pop(stream_id, None)
                    return True, True, None, None, None

                prefixed = f"{tag} {text}".rstrip()
                seg.data = prefixed  # type: ignore[attr-defined]
                modified_segments = message.message_segments
                break

            if not modified_segments:
                return True, True, None, None, None

            message.modify_message_segments(modified_segments, suppress_warning=True)

            _ACTION_CHECK_PENDING_TAG_BY_STREAM.pop(stream_id, None)
            _ACTION_CHECK_CONTEXT_BY_STREAM.pop(stream_id, None)

            return True, True, "动作检定标签已加到发送消息", None, message
        except Exception as e:
            logger.error(f"动作检定 POST_SEND_PRE_PROCESS 处理失败: {e}", exc_info=True)
            return True, True, None, None, None


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
            
            # 统一用户ID处理和消息获取方式
            user_id = ""
            message = None
            
            # 使用标准化的用户ID提取方法
            from .services.message_service import MessageService

            # v7修复：从ChatStream.context的最后消息的reply字段获取回复目标用户ID
            # v6修复失败原因：ChatStream.user_info.user_id是聊天流用户，不是Bot回复的目标用户
            # v7正确方案：通过last_message.reply获取被回复用户ID
            if hasattr(event_data, 'stream_id') and event_data.stream_id:
                try:
                    from src.chat.message_receive.chat_stream import get_chat_manager
                    chat_manager = get_chat_manager()
                    target_stream = chat_manager.get_stream(event_data.stream_id)

                    if target_stream and target_stream.context:
                        last_message = target_stream.context.get_last_message()

                        if last_message:
                            # 检查是否有回复关系（群聊@回复场景）
                            if hasattr(last_message, 'reply') and last_message.reply:
                                # Bot回复给了特定用户，获取被回复用户的ID
                                raw_user_id = last_message.reply.message_info.user_info.user_id
                                user_id = MessageService.normalize_user_id(raw_user_id)
                                message = event_data
                                logger.debug(f"从reply字段获取目标用户ID: {user_id} (原始: {raw_user_id})")
                            else:
                                # 没有@回复，则目标是当前消息的发送者
                                raw_user_id = last_message.message_info.user_info.user_id
                                user_id = MessageService.normalize_user_id(raw_user_id)
                                message = event_data
                                logger.debug(f"从当前消息发送者获取目标用户ID: {user_id} (原始: {raw_user_id})")
                except Exception as e:
                    logger.warning(f"从ChatStream获取用户ID失败: {str(e)}")

            # 如果从stream_id获取失败，fallback到原有逻辑
            if not user_id:
                # 优先使用reply对象（群聊回复场景）
                # 在群聊中，当Bot回复某个用户时，reply.user_id是被回复的目标用户
                if hasattr(event_data, 'reply') and event_data.reply and hasattr(event_data.reply, 'user_id'):
                    raw_user_id = event_data.reply.user_id
                    user_id = MessageService.normalize_user_id(raw_user_id)
                    message = event_data
                    logger.debug(f"从reply对象提取用户ID: {user_id} (原始: {raw_user_id})")
                elif hasattr(event_data, 'message_base_info'):
                    message = event_data
                    raw_user_id = message.message_base_info.get('user_id', '')
                    user_id = MessageService.normalize_user_id(raw_user_id)
                    logger.debug(f"从message_base_info提取用户ID: {user_id} (原始: {raw_user_id})")
                elif hasattr(event_data, 'user_id'):
                    raw_user_id = event_data.user_id
                    user_id = MessageService.normalize_user_id(raw_user_id)
                    message = event_data
                    logger.debug(f"从event_data.user_id提取用户ID: {user_id} (原始: {raw_user_id})")
                elif hasattr(event_data, 'plain_text'):
                    raw_user_id = getattr(event_data, 'user_id', '')
                    user_id = MessageService.normalize_user_id(raw_user_id)
                    message = event_data
                    logger.debug(f"从plain_text分支提取用户ID: {user_id} (原始: {raw_user_id})")
                else:
                    # 尝试从事件数据中提取消息
                    if hasattr(event_data, '__dict__'):
                        for attr_name in ['message', 'msg', 'data']:
                            if hasattr(event_data, attr_name):
                                potential_msg = getattr(event_data, attr_name)
                                if hasattr(potential_msg, 'user_id'):
                                    raw_user_id = potential_msg.user_id
                                    user_id = MessageService.normalize_user_id(raw_user_id)
                                    message = potential_msg
                                    logger.debug(f"从{attr_name}属性提取用户ID: {user_id}")
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

            # 从主程序数据库获取实际的消息ID
            message_id = None
            message_timestamp = None
            
            # 尝试从多个来源获取时间戳
            if hasattr(message, 'message_base_info') and message.message_base_info:
                # 从 message_base_info 获取时间戳
                if 'time' in message.message_base_info:
                    message_timestamp = float(message.message_base_info['time'])
                elif 'timestamp' in message.message_base_info:
                    message_timestamp = float(message.message_base_info['timestamp'])
                elif 'create_time' in message.message_base_info:
                    message_timestamp = float(message.message_base_info['create_time'])
                logger.debug(f"从 message_base_info 获取时间戳: {message_timestamp}")
            
            # 如果没有时间戳，使用当前时间
            if not message_timestamp:
                import time
                message_timestamp = time.time()
                logger.debug(f"使用当前时间作为时间戳: {message_timestamp}")
            
            # 尝试获取主程序message_id
            if self.weight_service.db_service and self.weight_service.db_service.is_connected():
                message_id = self.weight_service.db_service.get_main_message_id(user_id, message_timestamp)
                if message_id:
                    logger.debug(f"获取到主程序实际消息ID: {message_id}")
                else:
                    logger.debug(f"无法从主程序数据库获取message_id，用户: {user_id}, 时间戳: {message_timestamp}")
            
            # 如果无法获取到主程序ID，使用当前时间戳作为临时ID（向后兼容）
            if not message_id:
                import time
                message_id = f"temp_{user_id}_{int(message_timestamp)}"
                logger.warning(f"无法获取主程序消息ID，使用临时ID: {message_id}")
                
            # 记录调试信息
            logger.debug(f"消息处理详情 - 用户: {user_id}, 时间戳: {message_timestamp}, message_id: {message_id}, 内容: {message_content[:50]}...")

            # 检查消息是否已处理（基于message_id）
            is_processed = self.message_service.is_message_processed(user_id, message_id)
            logger.debug(f"查重检查 - 用户: {user_id}, message_id: {message_id}, 是否已处理: {is_processed}")
            if is_processed:
                logger.debug(f"用户 {user_id} 的消息 {message_id} 已处理，跳过")
                return CustomEventHandlerResult(message="消息已处理，跳过")

            logger.debug(f"开始处理用户 {user_id} 的消息: {message_content[:50]}...")

            # 获取配置
            history_config = self.plugin_config.get("history", {})
            max_messages = history_config.get("max_messages", 20)

            # 获取过滤后的历史上下文（用于权重评估）
            history_context, message_ids_in_context = self.weight_service.get_filtered_messages(user_id, limit=max_messages)
            logger.info(f"获取到过滤后的历史上下文，长度: {len(history_context)} 字符，包含消息 {len(message_ids_in_context)} 条")

            # v5修复：获取到消息后立即标记为已处理，无论后续流程如何
            # 这样可以保证下次只获取最新的、未处理的消息
            for msg_id in message_ids_in_context:
                self.message_service.record_processed_message(user_id, msg_id)
            logger.info(f"已批量标记 {len(message_ids_in_context)} 条消息为已处理（获取后立即标记）")

            # 检查过滤后的上下文是否为空，如果为空则跳过权重评估
            weight_success = False
            weight_score = 0.0
            weight_level = "low"
            
            if len(history_context.strip()) == 0:
                logger.debug(f"过滤后的上下文为空，跳过权重评估 - 用户: {user_id}")
            else:
                # 在异步任务中进行权重评估
                logger.debug(f"开始评估消息权重 - 用户: {user_id}")
                weight_success, weight_score, weight_level = await self.weight_service.evaluate_message(
                    user_id, message_id, message_content, history_context
                )

                if not weight_success:
                    logger.warning(f"权重评估失败: {weight_level}")
                else:
                    logger.info(f"权重评估成功 - 分数: {weight_score}, 等级: {weight_level}")
                    # v5修复：标记逻辑已移到获取消息后立即执行，此处不再重复标记

            # 根据权重等级决定是否更新印象
            impression_updated = False
            should_update_impression = False
            
            if len(history_context.strip()) == 0:
                logger.info(f"过滤后的上下文为空，跳过印象更新 - 用户: {user_id}")
                should_update_impression = False
            elif weight_success:
                # 有权重评估结果时，根据权重等级决定
                filter_mode = self.weight_service.filter_mode
                high_threshold = self.weight_service.high_threshold
                medium_threshold = self.weight_service.medium_threshold
                
                if filter_mode == "disabled":
                    should_update_impression = True
                elif filter_mode == "selective":
                    should_update_impression = weight_score >= high_threshold
                elif filter_mode == "balanced":
                    should_update_impression = weight_score >= medium_threshold
                
                logger.debug(f"权重筛选检查 - 模式: {filter_mode}, 分数: {weight_score}, 阈值: {high_threshold}/{medium_threshold}, 是否更新印象: {should_update_impression}")
            else:
                logger.debug(f"权重评估失败，跳过印象更新")
                should_update_impression = False

            # 更新印象 - 权重评估通过后再次获取最新的过滤上下文
            if should_update_impression:
                try:
                    # 再次获取最新的过滤上下文用于印象构建
                    latest_context, latest_message_ids = self.weight_service.get_filtered_messages(user_id, limit=max_messages)
                    logger.debug(f"获取到最新的过滤上下文用于印象构建，长度: {len(latest_context)} 字符，包含消息 {len(latest_message_ids)} 条")

                    logger.debug(f"开始构建印象 - 用户: {user_id}")
                    success, impression_result = await self.text_impression_service.build_impression(
                        user_id, message_content, latest_context
                    )
                    if success:
                        impression_updated = True
                        logger.info(f"印象更新成功")
                        # v4修复：批量标记逻辑已移到权重评估成功后，此处不再重复标记
                    else:
                        logger.warning(f"印象更新失败")
                except Exception as e:
                    logger.error(f"印象更新异常: {str(e)}")
            else:
                logger.debug(f"权重等级不满足印象更新条件 (分数: {weight_score}, 等级: {weight_level})，跳过印象更新")

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

            # 输出最终处理统计
            logger.debug(f"用户 {user_id} 消息处理完成: 印象更新 {impression_updated}, 好感度更新 {affection_updated}, 权重分数 {weight_score:.1f}, 等级 {weight_level}")

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
                default="2.3.6",
                description="配置文件版本"
            )
        },

        # =============================================================================
        # LLM 配置
        # =============================================================================
        "llm_provider": {
            "provider_type": ConfigField(
                type=str,
                default="main",
                description="LLM提供商: main(主程序任务组)/openai/custom"
            ),
            "task_group": ConfigField(
                type=str,
                default="utils",
                description="当 provider_type=main 时使用的主程序模型任务组（默认 utils）"
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
        # 权限配置
        # =============================================================================
        "permissions": {
            "admin": ConfigField(
                type=list,
                default=[],
                description="允许使用插件命令的管理员列表（platform:user_id 或 user_id）"
            )
        },

        # =============================================================================
        # 动作检定配置（由 planner 决定结果）
        # =============================================================================
        "action_check": {
            "enabled": ConfigField(
                type=bool,
                default=False,
                description="启用动作检定功能（planner 同选 reply+action_check，并给出检定结果）"
            ),
            "show_roll_result": ConfigField(
                type=bool,
                default=False,
                description="在最终回复开头展示检定结果标签（通过发送阶段加前缀，实时生效）"
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
            ),
            "max_cache_size": ConfigField(
                type=int,
                default=1000,
                description="内存缓存最大消息数"
            ),
            "max_weight_records": ConfigField(
                type=int,
                default=100,
                description="权重记录最大保存数"
            ),
            "history_summary_count": ConfigField(
                type=int,
                default=5,
                description="历史摘要显示消息数"
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
                default="基于对话记录生成用户画像和印象描述。现有印象：{existing_impression} 历史对话：{history_context} 当前消息：{message} 要求提炼核心性格特征和行为模式，描述要客观简洁直接，避免使用生硬的心理学术语，保留用户的独特性和个人特点。100字左右",
                description="印象分析提示词模板"
            ),
            
            "affection_template": ConfigField(
                type=str,
                default="你是NLP语义分析专家。请基于语用学特征对消息进行严格分类(friendly/neutral/negative)。【判定界限】1.Friendly(友好)：必须包含显性积极情绪（如喜爱、兴奋、感激）、亲昵称呼、幽默或颜文字，注意：仅包含基础礼貌用语（如“你好/麻烦/谢谢”）但无情绪波动的，不属于此列；2.Neutral(中性)：指客观陈述、事务性指令（如“帮我查一下”）、信息确认，侧重于功能性交互；3.Negative(负面)：包含敌意、愤怒、嘲讽（反语/阴阳怪气）、不耐烦或严厉批评。返回格式：TYPE: friendly/neutral/negative; REASON: 基于语气和用词的专业判定; 消息: {message}",
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
                
                # 检查并添加新字段（数据库迁移）
                self._migrate_database()
                
                self.db_initialized = True
                logger.info(f"数据库初始化成功: {DB_PATH}")
                
                # 验证表是否创建成功
                tables = db.get_tables()
                logger.info(f"已创建的表: {tables}")
                
            except Exception as e:
                logger.error(f"数据库初始化失败: {str(e)}")
                raise e

    def _migrate_database(self):
        """数据库迁移 - 添加新字段"""
        try:
            from .models import ImpressionMessageRecord
            
            # 检查 content_hash 字段是否存在
            cursor = db.execute_sql("PRAGMA table_info(impression_message_records)")
            columns = [row[1] for row in cursor.fetchall()]
            
            if 'content_hash' not in columns:
                logger.info("检测到缺少 content_hash 字段，开始数据库迁移...")
                
                # 添加 content_hash 字段
                db.execute_sql("ALTER TABLE impression_message_records ADD COLUMN content_hash TEXT")
                
                # 为新字段创建索引
                db.execute_sql("CREATE INDEX IF NOT EXISTS impression_message_records_user_content_hash ON impression_message_records(user_id, content_hash)")
                
                logger.info("数据库迁移完成：已添加 content_hash 字段和索引")
            else:
                logger.debug("content_hash 字段已存在，跳过迁移")
                
        except Exception as e:
            logger.error(f"数据库迁移失败: {str(e)}")
            # 不抛出异常，允许插件继续运行

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件组件列表"""
        self.init_db()

        components = []

        # 添加事件处理器
        components.append((ImpressionUpdateHandler.get_handler_info(), ImpressionUpdateHandler))
        components.append((ActionCheckPlannerPromptHandler.get_handler_info(), ActionCheckPlannerPromptHandler))
        components.append((ActionCheckPostLLMHandler.get_handler_info(), ActionCheckPostLLMHandler))
        components.append((ActionCheckAfterLLMHandler.get_handler_info(), ActionCheckAfterLLMHandler))
        components.append((ActionCheckPostSendPrefixHandler.get_handler_info(), ActionCheckPostSendPrefixHandler))

        # 动作组件（用于让 planner 显式选择 action_check）
        components.append((ActionCheckAction.get_action_info(), ActionCheckAction))

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
                (ListImpressionsCommand.get_command_info(), ListImpressionsCommand),
                (ToggleActionCheckCommand.get_command_info(), ToggleActionCheckCommand),
                (ToggleActionCheckShowResultCommand.get_command_info(), ToggleActionCheckShowResultCommand),
            ])

        return components
