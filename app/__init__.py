"""填料吸收塔传质高度核算 HTTP 服务。

模块职责划分：

- ``app.validation``  请求校验（类型 / 取值范围 / 完整性）
- ``app.balance``     全塔物料衡算与操作线
- ``app.nog``         NOG 积分（对数平均推动力封闭式 + λ=1 退化支）
- ``app.engine``      把上述模块串成一次完整正算，并做夹点判定
- ``app.invert``      反解内核（目标/塔高 -> 单调二分求根 + 可行性边界）
- ``app.profiles``    具名工况档的持久化（JSON + 文件锁 + 原子写）
- ``app.service``     核算编排（工况档解析、单条 / 批量、正算与反解）
- ``app.routers``     HTTP 接口层（calc / invert / profiles）
"""

__version__ = "1.1.0"
