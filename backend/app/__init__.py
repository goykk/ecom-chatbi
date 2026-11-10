"""电商 ChatBI 后端应用包。"""

# 本地开发时先加载 deploy/.env，保证直接跑脚本（eval / 命令行）也能拿到配置
from app.core.config import _load_env_file

_load_env_file()
