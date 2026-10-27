"""
关键词过滤模块（从 qqtranspond 项目迁移）。

根据配置的关键词列表，判断一条说说是否需要转发。
支持格式：
- "关键词"   -> 包含即匹配
- "+关键词"  -> 必须包含（默认）
- "-排除词"  -> 排除（不可包含）
"""
import re
import logging
from typing import List

logger = logging.getLogger("flyer.filter")


class KeywordFilter:
    """关键词过滤器，支持正向匹配和排除匹配"""

    def __init__(self, keywords: List[str]):
        """
        :param keywords: 关键词列表
        """
        self.include_patterns: List[re.Pattern] = []
        self.exclude_patterns: List[re.Pattern] = []

        for kw in keywords or []:
            kw = str(kw).strip()
            if not kw:
                continue
            if kw.startswith("-"):
                # 排除词
                pattern = kw[1:].strip()
                if pattern:
                    self.exclude_patterns.append(re.compile(re.escape(pattern)))
            else:
                # 包含词，去掉可选的 +
                if kw.startswith("+"):
                    kw = kw[1:].strip()
                if kw:
                    self.include_patterns.append(re.compile(re.escape(kw)))

    def match(self, text: str) -> bool:
        """
        判断文本是否匹配过滤条件

        :return: True 表示需要转发，False 表示忽略
        """
        if not text:
            return False

        # 如果有包含关键词列表，至少匹配一个
        if self.include_patterns:
            if not any(p.search(text) for p in self.include_patterns):
                return False

        # 如果匹配了排除词，则不转发
        for p in self.exclude_patterns:
            if p.search(text):
                logger.debug(f"排除词 '{p.pattern}' 匹配，跳过")
                return False

        return True

    @property
    def is_active(self) -> bool:
        """是否有激活的过滤规则"""
        return bool(self.include_patterns) or bool(self.exclude_patterns)

    @staticmethod
    def format_content(post: dict) -> str:
        """将说说格式化为可读文本（用于转发）"""
        content = post.get("content", "") or ""
        pics = post.get("pictures", [])
        time_str = post.get("create_time", "")
        uname = post.get("nickname", post.get("uin", ""))

        # 构建消息
        lines = [
            f"📝 {uname} 的QQ空间动态",
            f"🕐 {time_str}",
            "———————————————",
        ]
        if content:
            lines.append(content)
        if pics:
            lines.append(f"📷 共 {len(pics)} 张图片")

        return "\n".join(lines)
