"""Settings 持久化收口：WebUI 读写 config 的单一入口。

``erza.config.loader`` 在 ``erza/channels`` 下仅经本模块引用一次，
其余 settings api / handler 一律通过 :class:`SettingsStore` 读写。

Store 无状态：``read`` 每次调用都从磁盘全新加载（等价原逐次直接读
config 的语义），``write`` 等价直接落盘。所有 loader 名字都在调用时
经模块命名空间解析，测试对 ``erza.config.loader`` 相关符号的
monkeypatch 依旧生效。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import erza.config.loader as _loader
from erza.config.schema import Config


class SettingsStore:
    """Config 持久化入口：read=全新加载，write=落盘，update=读改写一体化。"""

    def read(self) -> Config:
        return _loader.load_config()

    def write(self, config: Config) -> None:
        _loader.save_config(config)

    def update(self, fn: Callable[[Config], None]) -> Config:
        config = self.read()
        fn(config)
        self.write(config)
        return config


def get_config_path() -> Any:
    """转发 ``erza.config.loader.get_config_path``（调用时解析，可被 patch）。"""
    return _loader.get_config_path()


def resolve_config_env_vars(config: Config) -> Config:
    """转发 ``erza.config.loader.resolve_config_env_vars``（调用时解析，可被 patch）。"""
    return _loader.resolve_config_env_vars(config)


__all__ = ["SettingsStore", "get_config_path", "resolve_config_env_vars"]
