"""Maigret 中文增强子包（cn overlay）。

设计约束（见《开发文档 v2.0》§5.1）：
    上游文件零改动，所有定制收进本包，保证能 merge 上游更新。

本包的四个基座模块均为**纯标准库实现**，不 import aiohttp / 上游模块，
因此可以脱离 maigret 运行时独立测试：

    model.py     站点数据模型 Site（共享契约）
    schema.py    四字段校验，危险规则在配置期拒绝
    overlay.py   三级合并：sites_cn.json > overrides.json > 上游 data.json
    verdict.py   四字段判定引擎（先判被打断，再判存在性）
    scoring.py   置信度模型（status_only 固定 0.5，不进高置信档）
"""

__all__ = ["model", "schema", "overlay", "verdict", "scoring"]
