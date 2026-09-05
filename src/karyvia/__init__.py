"""Karyvia：轻量、模块化、可扩展的 Agent Kernel。

职责：作为唯一 Python 包的根，划分 contracts / kernel / sdk / builtins /
runtime / embed 六层。
不负责：任何实现。本模块保持零依赖、零副作用，`import karyvia` 不得触发
配置读取、日志配置或子模块导入。
"""
