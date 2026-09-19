"""插桩5：定期复盘分析。**本轮返回空结果**。"""

SOURCE = '''
# 复盘需要历史快照与回测数据（文档 01 §AI优化子流程 步骤1），
# 本轮 AI 优化子流程未实现（设计文档 §3「不做」），故返回空结果。
def run(ctx):
    return {"analysis_result": {"status": "not_implemented"}, "bad_case_list": []}
'''.lstrip()
