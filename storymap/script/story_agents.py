"""
职责：负责“故事生成”（调用 LLM），不包含地图或距离相关逻辑。
提示词从 docs/ 目录加载，便于集中管理与调优。
"""
import argparse
import json
import os
import re
import requests
import urllib3
from typing import Dict, List, Optional

from dotenv import load_dotenv

# 禁用 urllib3 的不安全请求警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

local_env = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=local_env)

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
    - 调用 Qveris 的 Execute Tool 接口来执行大模型对话
    - 兼容 OpenAI 格式的 messages 输入
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
        - LLM_API_KEY   -> Qveris API Key
        - LLM_BASE_URL  -> Qveris API Base URL (例如 https://qveris.ai/api/v1)
        """
        self.model = model or os.getenv("LLM_MODEL_ID")
        self.event_callback = event_callback
        self.apiKey = apiKey or os.getenv("LLM_API_KEY")
        self.baseUrl = baseUrl or os.getenv("LLM_BASE_URL")
        # Increase default timeout to 300 seconds (5 minutes)
        self.timeout = timeout or int(os.getenv("LLM_TIMEOUT", "300"))
        
        # Qveris Tool ID for ZHIPU GLM-4 chat completions
        self.tool_id = "bigmodel.chat.completions.create.v4.bbf1f5ab"

        if not self.model or not self.apiKey or not self.baseUrl:
            raise ValueError("模型ID、API密钥和服务地址必须被提供或在.env文件中定义。")

    def _emit(self, message: str) -> None:
        if not self.event_callback:
            return
        try:
            self.event_callback(message)
        except Exception:
            pass

    def think(self, messages: List[Dict[str, str]], temperature: float = 0) -> Optional[str]:
        """
        通过 Qveris Execute Tool 接口调用大模型。
        """
        import time
        max_retries = 3
        
        print(f"🧠 正在调用 {self.model} 模型 (via Qveris)...")
        self._emit(f"🧠 正在调用 {self.model} 模型 (via Qveris)...")

        url = f"{self.baseUrl.rstrip('/')}/tools/execute"
        headers = {
            "Authorization": f"Bearer {self.apiKey}",
            "Content-Type": "application/json"
        }
        
        # 构造传递给工具的参数
        params_to_tool = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.apiKey}" 
        }

        payload = {
            "tool_id": self.tool_id,
            "parameters": params_to_tool
        }

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                # Qveris execute tool 接口通常不支持流式返回，这里使用同步调用
                # 禁用 SSL 验证以解决证书错误
                resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout, verify=False)
                resp.raise_for_status()
                
                data = resp.json()
                
                if not data.get("success"):
                    error_msg = data.get("error_message") or "Unknown error"
                    raise RuntimeError(f"Qveris execution failed: {error_msg}")

                tool_result = data.get("result", {}).get("data", {})
                
                # 解析 OpenAI 格式的响应
                content = ""
                if isinstance(tool_result, dict):
                    choices = tool_result.get("choices", [])
                    if choices and len(choices) > 0:
                        message = choices[0].get("message", {})
                        content = message.get("content", "")
                
                # 如果 result.data 直接是字符串（某些工具可能直接返回内容）
                if not content and isinstance(tool_result, str):
                    content = tool_result

                if content:
                    print(content)
                    self._emit(f"✅ 大语言模型响应成功")
                    return content
                else:
                    print("⚠️ 模型返回内容为空")
                    # 空内容不视为错误，直接返回空字符串
                    return ""

            except Exception as e:
                last_error = e
                print(f"⚠️ 第 {attempt}/{max_retries} 次尝试失败: {e}")
                if attempt < max_retries:
                    wait_time = 2 * attempt  # 简单的指数退避
                    print(f"⏳ {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                else:
                    print(f"❌ 调用LLM API最终失败: {e}")
                    self._emit(f"❌ 调用LLM API最终失败: {e}")
        
        return None


def _read_prompt(relpath: str) -> str:
    """
    读取 docs/ 目录下的提示词文件内容。
    """
    root = os.path.dirname(os.path.abspath(__file__))
    # script/../docs -> storymap/docs
    prompt_path = os.path.join(root, "..", "docs", relpath)
    if not os.path.exists(prompt_path):
         # Fallback for when running from project root
         root_proj = _project_root()
         prompt_path = os.path.join(root_proj, "storymap", "docs", relpath)
    
    with open(prompt_path, "r", encoding="utf-8") as f:
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
    保存 Markdown 到 examples/story/ 目录，若存在则覆盖。
    """
    root = _project_root()
    base = os.path.join(root, "storymap", "examples", "story")
    os.makedirs(base, exist_ok=True)
    filename = f"{person}.md"
    path = os.path.join(base, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"✅ 人物生平已保存: {path}")
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
