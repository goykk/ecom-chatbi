"""运行配置：全部来自环境变量，不在代码里写死地址与密钥。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_env_file() -> None:
    """本地开发时自动读取 deploy/.env；容器里没有该文件，走 compose 注入的环境变量。

    `override=False`：已存在的环境变量优先，避免把 CI / 容器配置被文件覆盖。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # 没装 dotenv 时静默跳过
        return
    candidates = [
        Path(__file__).resolve().parents[3] / "deploy" / ".env",
        Path.cwd() / ".env",
    ]
    for path in candidates:
        if path.exists():
            load_dotenv(path, override=False)
            break


_load_env_file()


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    redis_url: str
    llm_model: str
    cors_origins: tuple[str, ...]
    enable_db: bool

    @classmethod
    def from_env(cls) -> "Settings":
        origins = os.getenv("CORS_ORIGINS", "*")
        return cls(
            app_env=os.getenv("APP_ENV", "dev"),
            database_url=os.getenv(
                "DATABASE_URL",
                "mysql+pymysql://chatbi_ro:chatbi_ro@127.0.0.1:3306/ecom_chatbi?charset=utf8mb4",
            ),
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            llm_model=os.getenv("LLM_MODEL", "deepseek-chat"),
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
            enable_db=os.getenv("ENABLE_DB", "1") != "0",
        )


settings = Settings.from_env()
