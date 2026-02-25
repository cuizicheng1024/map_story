"""
职责：负责“故事生成”（调用 LLM），不包含地图或距离相关逻辑。
提示词从 docs/ 目录加载，便于集中管理与调优。
"""
import argparse
import json
import os
import re
from typing import Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


local_env = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=local_env)
load_dotenv(dotenv_path=os.path.join(_project_root(), ".env"))

_MAX_TEXT_LEN = 200


def _validate_person(text: object) -> Optional[str]:
    if not isinstance(text, str):
        return "输入必须是字符串"
    cleaned = text.strip()
    if not cleaned:
        return "输入不能为空"
    if len(cleaned) > _MAX_TEXT_LEN:
        return f"输入过长（最多 {_MAX_TEXT_LEN} 字符）"
    return None


class StoryAgentLLM:
    """
    主要职责：
    - 统一管理模型 ID、API Key、Base URL 等基础配置
    - 对兼容 OpenAI 接口的服务发起对话请求
    - 默认使用流式（stream=True）方式逐块打印模型响应
    """
    def __init__(
        self,
        model: Optional[str] = None,
        apiKey: Optional[str] = None,
        baseUrl: Optional[str] = None,
        timeout: Optional[int] = None,
        event_callback: Optional[callable] = None,
    ):
        """
        初始化客户端。

        优先使用传入的参数；如果某个参数为 None，则会回退到环境变量：
        - LLM_MODEL_ID  -> 模型 ID
        - LLM_API_KEY   -> API Key
        - LLM_BASE_URL  -> 服务地址（兼容 OpenAI 协议的网关）
        - LLM_TIMEOUT   -> 请求超时时间（秒），默认 60
        """
        self.model = model or os.getenv("LLM_MODEL_ID")
        self.event_callback = event_callback
        apiKey = apiKey or os.getenv("LLM_API_KEY")
        baseUrl = baseUrl or os.getenv("LLM_BASE_URL")
        timeout = timeout or int(os.getenv("LLM_TIMEOUT", "60"))

        if not self.model or not apiKey or not baseUrl:
            raise ValueError("模型ID、API密钥和服务地址必须被提供或在.env文件中定义。")

        self.client = OpenAI(api_key=apiKey, base_url=baseUrl, timeout=timeout)

    def _emit(self, message: str) -> None:
        if not self.event_callback:
            return
        try:
            self.event_callback(message)
        except Exception:
            pass

    def think(self, messages: List[Dict[str, str]], temperature: float = 0) -> Optional[str]:
        """
        调用大语言模型进行“思考”，并以流式方式输出与返回完整结果。

        参数：
        - messages: 聊天历史，格式与 OpenAI ChatCompletion 接口一致
        - temperature: 采样温度，数值越大回答越发散，默认 0（更稳定）

        返回：
        - 模型完整输出的字符串；如果发生错误则返回 None
        """
        print(f"🧠 正在调用 {self.model} 模型...")
        self._emit(f"🧠 正在调用 {self.model} 模型...")
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                stream=True,
            )
            print("✅ 大语言模型响应成功:")
            collected: List[str] = []
            for chunk in response:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None) or ""
                if not content:
                    continue
                print(content, end="", flush=True)
                collected.append(content)
            print()
            result = "".join(collected)
            if result:
                self._emit(f"✅ 大语言模型响应成功: {result}")
            else:
                self._emit("✅ 大语言模型响应成功")
            return result

        except Exception as e:
            print(f"❌ 调用LLM API时发生错误: {e}")
            self._emit(f"❌ 调用LLM API时发生错误: {e}")
            return None


def _read_prompt(relpath: str) -> str:
    """
    读取 docs/ 目录下的提示词文件内容。
    """
    root = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(root, "..", "docs", relpath), "r", encoding="utf-8") as f:
        return f.read()


def generate_historical_markdown(llm: "StoryAgentLLM", person: str) -> Optional[str]:
    """
    生成指定人物的生平 Markdown。
    """
    system_prompt = _read_prompt("story_system_prompt.md")
    user_prompt = f"请整理历史人物「{person}」的生平信息，并按要求输出。"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return llm.think(messages, temperature=0.1)


def extract_historical_figures(llm: "StoryAgentLLM", text: str) -> List[str]:
    """
    从输入文本中抽取历史人物名称列表。
    """
    if not isinstance(text, str):
        return []
    sys_prompt = _read_prompt("extract_names_prompt.md")
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": text},
    ]
    raw = llm.think(messages, temperature=0)
    if not raw:
        return []
    try:
        data = json.loads(raw.strip())
        if isinstance(data, list):
            names = [str(x).strip() for x in data if str(x).strip()]
            return list(dict.fromkeys(names))
    except Exception:
        pass
    cleaned = raw.strip()
    return [cleaned] if cleaned else []


def save_markdown(person: str, content: str) -> str:
    """
    将人物生平 Markdown 写入 story/ 目录并返回文件路径。
    """
    root = _project_root()
    folder = os.path.join(root, "story")
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r'[\\\\/:*?"<>|]', "_", str(person or "")).strip()
    if not safe:
        safe = "未命名人物"
    path = os.path.join(folder, f"{safe}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def run_interactive(llm: "StoryAgentLLM") -> None:
    """
    交互式输入人物并生成 Markdown。
    """
    while True:
        try:
            name = input("请输入历史人物（q/quit/exit 退出）：").strip()
        except EOFError:
            break
        if not name:
            continue
        err = _validate_person(name)
        if err:
            print(err)
            continue
        if name.lower() in {"q", "quit", "exit"}:
            print("已退出。")
            break
        targets = extract_historical_figures(llm, name)
        if not targets:
            print("未识别到历史人物，请重试。")
            continue
        for person in targets:
            md = generate_historical_markdown(llm, person)
            if md:
                saved = save_markdown(person, md)
                print(f"已生成：{saved}")
                print(md)
            else:
                print(f"未取得「{person}」结果。")


def main():
    parser = argparse.ArgumentParser(
        description="基于环境变量配置的 LLM，生成历史人物的 Markdown 生平信息。"
    )
    parser.add_argument(
        "-p", "--person", help="历史人物姓名，例如：李白、杜甫、诸葛亮", required=False
    )
    args = parser.parse_args()

    if args.person:
        try:
            err = _validate_person(args.person)
            if err:
                print(err)
                return
            client = StoryAgentLLM()
            targets = extract_historical_figures(client, args.person)
            if not targets:
                print("未识别到历史人物。")
                return
            for person in targets:
                md = generate_historical_markdown(client, person)
                if md:
                    saved = save_markdown(person, md)
                    print(f"已生成：{saved}")
                    print(md)
        except ValueError as e:
            print(e)
        return

    try:
        client = StoryAgentLLM()
        run_interactive(client)
    except ValueError as e:
        print(e)


if __name__ == "__main__":
    main()
