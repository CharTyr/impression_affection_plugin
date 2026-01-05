"""
命令组件 - 管理命令
"""

from typing import Optional, Sequence
from src.plugin_system import BaseCommand

from ..models import UserImpression, UserMessageState


def _is_admin_platform_user_id(platform: str, user_id: str, admin_list: Sequence[str]) -> bool:
    if not platform or not user_id:
        return False

    platform_user_id = f"{platform}:{user_id}"
    return platform_user_id in admin_list or user_id in admin_list


class AdminOnlyCommand(BaseCommand):
    def _is_admin(self) -> bool:
        try:
            admin_list = self.get_config("permissions.admin", []) or []
            if not isinstance(admin_list, list):
                return False

            platform = str(getattr(self.message.message_info, "platform", "") or "")
            user_info = getattr(self.message.message_info, "user_info", None)
            user_id = str(getattr(user_info, "user_id", "") or "")
            return _is_admin_platform_user_id(platform, user_id, admin_list)
        except Exception:
            return False

    def _silent_block_if_not_admin(self) -> Optional[tuple]:
        if not self._is_admin():
            return True, None, 2
        return None


class ViewImpressionCommand(AdminOnlyCommand):
    """查看印象命令"""

    command_name = "view_impression"
    command_description = "查看指定用户的印象和好感度"
    command_pattern = r"^/impression\s+(?:view|v)\s+(?P<user_id>\d+)$"

    async def execute(self) -> tuple:
        """执行查看印象"""
        if blocked := self._silent_block_if_not_admin():
            return blocked

        try:
            user_id = self.matched_groups.get("user_id")
            if not user_id:
                await self.send_text("请提供用户ID")
                return False, "请提供用户ID", 2

            # 从数据库获取印象
            impression = UserImpression.select().where(
                UserImpression.user_id == user_id
            ).first()

            if not impression:
                await self.send_text(f"暂无用户 {user_id} 的印象数据")
                return False, f"暂无用户 {user_id} 的印象数据", 2

            # 获取消息状态
            state = UserMessageState.get_or_create(user_id=user_id)[0]

            # 获取印象摘要
            impression_summary = impression.get_impression_summary()

            message = f"""
用户印象信息 (ID: {user_id})
━━━━━━━━━━━━━━━━━━━━━━
印象: {impression_summary}

好感度: {impression.affection_score:.1f}/100 ({impression.affection_level})
累计消息: {impression.message_count} 条
总消息: {state.total_messages} 条
更新时间: {impression.updated_at.strftime('%Y-%m-%d %H:%M:%S')}
━━━━━━━━━━━━━━━━━━━━━━
            """.strip()

            await self.send_text(message)
            return True, None, 2

        except Exception as e:
            error_msg = f"查看印象失败: {str(e)}"
            await self.send_text(error_msg)
            return False, error_msg, 2


class SetAffectionCommand(AdminOnlyCommand):
    """手动设置好感度命令"""

    command_name = "set_affection"
    command_description = "手动调整用户好感度"
    command_pattern = r"^/impression\s+(?:set|s)\s+(?P<user_id>\d+)\s+(?P<score>\d+)$"

    async def execute(self) -> tuple:
        """执行设置好感度"""
        if blocked := self._silent_block_if_not_admin():
            return blocked

        try:
            user_id = self.matched_groups.get("user_id")
            score_str = self.matched_groups.get("score")

            if not user_id or not score_str:
                await self.send_text("用法: /impression set <user_id> <score>")
                return False, "参数错误", 2

            try:
                score = float(score_str)
                if not (0 <= score <= 100):
                    await self.send_text("好感度分数必须在0-100之间")
                    return False, "分数超出范围", 2
            except ValueError:
                await self.send_text("好感度分数必须是数字")
                return False, "分数格式错误", 2

            # 获取或创建印象记录
            impression, created = UserImpression.get_or_create(user_id=user_id)

            # 更新好感度
            impression.affection_score = score
            impression.affection_level = self._get_affection_level(score)
            impression.save()

            action = "创建" if created else "更新"
            await self.send_text(f"{action}用户 {user_id} 的好感度为: {score:.1f}/100 ({impression.affection_level})")

            return True, f"{action}好感度成功", 2

        except Exception as e:
            error_msg = f"设置好感度失败: {str(e)}"
            await self.send_text(error_msg)
            return False, error_msg, 2

    def _get_affection_level(self, score: float) -> str:
        """根据分数获取好感度等级"""
        from ..utils import get_affection_level
        return get_affection_level(score)


class ListImpressionsCommand(AdminOnlyCommand):
    """列出所有印象命令"""

    command_name = "list_impressions"
    command_description = "列出所有用户的印象和好感度"
    command_pattern = r"^/impression\s+(?:list|ls)$"

    async def execute(self) -> tuple:
        """执行列出印象"""
        if blocked := self._silent_block_if_not_admin():
            return blocked

        try:
            # 获取所有印象
            impressions = UserImpression.select()

            if not impressions:
                await self.send_text("暂无用户印象数据")
                return True, "无数据", 2

            # 构建消息
            message = "用户印象列表\n"
            message += "━━━━━━━━━━━━━━━━━━━━━━\n"

            for imp in impressions:
                impression_summary = imp.get_impression_summary()
                message += f"\n用户: {imp.user_id}\n"
                message += f"印象: {impression_summary[:30]}...\n"
                message += f"好感度: {imp.affection_score:.1f}/100 ({imp.affection_level})\n"
                message += f"消息数: {imp.message_count}\n"
                message += f"更新: {imp.updated_at.strftime('%m-%d %H:%M')}\n"

            await self.send_text(message)
            return True, f"列出 {len(impressions)} 个用户印象", 2

        except Exception as e:
            error_msg = f"列出印象失败: {str(e)}"
            await self.send_text(error_msg)
            return False, error_msg, 2


class ToggleActionCheckCommand(AdminOnlyCommand):
    """动作检定总开关（仅内存，重启恢复默认）"""

    command_name = "toggle_action_check"
    command_description = "开启/关闭动作检定功能（仅管理员）"
    command_pattern = r"^/impression\s+roll\s+(?P<state>on|off|status)$"

    async def execute(self) -> tuple:
        if blocked := self._silent_block_if_not_admin():
            return blocked

        state = (self.matched_groups.get("state") or "").lower()
        action_check_cfg = self.plugin_config.setdefault("action_check", {})

        if state == "on":
            action_check_cfg["enabled"] = True
            await self.send_text("动作检定：已开启（重启后恢复配置默认值）")
        elif state == "off":
            action_check_cfg["enabled"] = False
            await self.send_text("动作检定：已关闭（重启后恢复配置默认值）")
        else:
            enabled = bool(action_check_cfg.get("enabled", False))
            await self.send_text(f"动作检定：{'开启' if enabled else '关闭'}")

        return True, None, 2


class ToggleActionCheckShowResultCommand(AdminOnlyCommand):
    """动作检定结果展示开关（仅内存，重启恢复默认）"""

    command_name = "toggle_action_check_show"
    command_description = "开启/关闭动作检定结果展示（仅管理员）"
    command_pattern = r"^/impression\s+rollshow\s+(?P<state>on|off|status)$"

    async def execute(self) -> tuple:
        if blocked := self._silent_block_if_not_admin():
            return blocked

        state = (self.matched_groups.get("state") or "").lower()
        action_check_cfg = self.plugin_config.setdefault("action_check", {})

        if state == "on":
            action_check_cfg["show_roll_result"] = True
            await self.send_text("动作检定结果展示：已开启（重启后恢复配置默认值）")
        elif state == "off":
            action_check_cfg["show_roll_result"] = False
            await self.send_text("动作检定结果展示：已关闭（重启后恢复配置默认值）")
        else:
            enabled = bool(action_check_cfg.get("show_roll_result", False))
            await self.send_text(f"动作检定结果展示：{'开启' if enabled else '关闭'}")

        return True, None, 2
