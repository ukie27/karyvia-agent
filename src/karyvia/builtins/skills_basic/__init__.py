"""内建 Skill 目录：发现 Agent Skills，并把精简目录贡献给模型上下文。

职责：扫描显式配置的 Skill 容器、校验 `SKILL.md` 的 name/description，并生成指向
`@skills/<name>/SKILL.md` 的渐进披露目录。
不负责：读取完整 Skill、执行脚本或管理安装；模型分别通过既有 `fs.*` 与 `shell.exec`
访问 Runtime 授权的同一组 Skill 根。
"""

from .provider import CAPABILITY_NAME, SkillCatalogProvider, setup
from .settings import CONFIG_ROOTS_KEY, MAX_DESCRIPTION_LENGTH, MAX_NAME_LENGTH

__all__ = [
    "CAPABILITY_NAME",
    "CONFIG_ROOTS_KEY",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_NAME_LENGTH",
    "SkillCatalogProvider",
    "setup",
]
