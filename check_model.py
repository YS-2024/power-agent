"""Explicit live smoke check: sends only synthetic teaching content, never user files."""

import tempfile
from pathlib import Path

from power_agent.agent import PowerAgent, load_env
from power_agent.knowledge import KnowledgeBase


def main():
    load_env(Path(__file__).resolve().parent / ".env")
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        kb = KnowledgeBase(root / "check.db")
        kb.add("模型连通性测试.txt", "# 功率因数\n功率因数 PF = 有功功率 P / 视在功率 S。该内容仅用于连通性测试。")
        try:
            agent = PowerAgent(kb, root / "reports")
        except ValueError as exc:
            print(str(exc))
            return 1
        if not agent.model_enabled:
            print("未配置模型，请填写 .env 后重试。")
            return 1
        print(f"测试 {agent.provider} / {agent.model}（发起一次真实模型请求）…", flush=True)
        result = agent.run("功率因数如何定义？请用一句话回答并引用来源。")
        if result["mode"] != "model":
            print(result["notice"] or "未获得模型回答。")
            return 1
        print("连接成功，模型返回了有效正文，引用编号校验通过。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
