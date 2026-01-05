"""
动作组件 - Action
"""

from typing import Tuple

from src.common.logger import get_logger
from src.plugin_system import BaseAction, ActionActivationType


logger = get_logger("impression_affection_action_check")


class ActionCheckAction(BaseAction):
    """
    动作检定 Action

    说明：
    - 本 Action 本身不做随机检定；检定结果由 planner 决定并通过 action_data 传入。
    - 该 Action 主要用于让 planner 有一个“显式动作”可选，并记录本次检定信息。
    """

    activation_type = ActionActivationType.ALWAYS
    parallel_action = False

    action_name = "action_check"
    action_description = "动作检定：当用户尝试抱抱/亲亲/摸头等互动时，planner 给出检定结果供回复参考"

    action_parameters = {
        "interaction": "动作名，例如：抱抱/亲亲/摸头",
        "chance": "成功率（0-100 的整数）",
        "result": "success 或 fail",
    }

    action_require = [
        "仅在你判断用户提出了直接动作互动时使用",
        "与 reply 动作同时选择，让回复模型根据检定结果自由发挥",
        "本动作不发送消息，只记录检定信息",
    ]

    associated_types = []

    async def execute(self) -> Tuple[bool, str]:
        try:
            interaction = self.action_data.get("interaction", "")
            chance = self.action_data.get("chance", "")
            result = self.action_data.get("result", "")
            display = f"动作检定: {interaction} {chance}% {result}".strip()

            await self.store_action_info(
                action_build_into_prompt=False,
                action_prompt_display=display,
                action_done=True,
            )
            return True, display
        except Exception as e:
            logger.error(f"{self.log_prefix} 动作检定 Action 执行失败: {e}", exc_info=True)
            return False, f"动作检定 Action 执行失败: {e}"
