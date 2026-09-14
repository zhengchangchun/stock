"""数据源适配器：纯函数，`raw -> 领域对象`，不含网络与状态。

出网一律经 `stocklab.data.http.HttpClient`；本包只做解析与单位换算。
"""
