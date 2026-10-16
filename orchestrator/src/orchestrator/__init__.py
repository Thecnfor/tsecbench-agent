"""L0 确定性编排器（architecture.md §0/§3）。

平台 API 规则（并发 ≤3、available 轮询、close 释放、flag 去重）
交给这里，绝不让大模型自由发挥；渗透解题交给 L1 solver 薄壳。
hint 接口不接（§4.4：扣分不可逆、期望收益为负），见 main.py 模块说明。
"""
