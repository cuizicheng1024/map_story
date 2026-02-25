import { useCallback, useEffect, useRef, useState } from "react";

const MAX_INPUT_LEN = 200;
const historyItems = ["曹操", "李白", "苏轼", "康熙", "唐三藏"];
const API_ENDPOINT = (() => {
  const globalBase = window.MAP_STORY_AI_ENDPOINT || window.MAP_STORY_API_BASE;
  if (typeof globalBase === "string" && globalBase.trim()) {
    return globalBase.replace(/\/+$/, "");
  }
  const origin = window.location.origin;
  const protocol = window.location.protocol;
  if (origin && origin !== "null" && protocol !== "file:") {
    return origin;
  }
  return "https://gapp.so/api/ai/gemini";
})();

const useDirectEndpoint = API_ENDPOINT.includes("/api/ai/") || API_ENDPOINT.includes("/ai/");

const buildApiUrl = (path) => {
  if (!path) return API_ENDPOINT;
  if (useDirectEndpoint) return API_ENDPOINT;
  return `${API_ENDPOINT}${path.startsWith("/") ? "" : "/"}${path}`;
};

const buildTaskUrl = (taskId) => {
  if (!taskId || useDirectEndpoint) return "";
  return buildApiUrl(`/task?id=${encodeURIComponent(taskId)}`);
};

const resolveFileUrl = (path) => {
  try {
    return new URL(path, window.location.href).href;
  } catch (err) {
    return path;
  }
};

const normalizeResultPayload = (data) => {
  if (!data) return null;
  if (data.result) return data.result;
  if (data.data) return data.data;
  return data;
};

const extractResultText = (result) => {
  if (!result) return "";
  if (typeof result === "string") return result;
  return result.text || result.content || result.message || result.answer || result.output || "";
};

export default function App() {
  const [messages, setMessages] = useState([
    {
      id: crypto.randomUUID(),
      type: "text",
      role: "assistant",
      text: "输入历史人物名称，我会检索相关事件并生成人物简介和足迹地图。"
    }
  ]);
  const [detailLines, setDetailLines] = useState([]);
  const [inputValue, setInputValue] = useState("");
  const lastQueueKeyRef = useRef("");
  const timerRef = useRef(null);
  const chatEndRef = useRef(null);

  useEffect(() => {
    if (chatEndRef.current) {
      chatEndRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, detailLines]);

  useEffect(() => () => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
    }
  }, []);

  const appendMessage = useCallback((payload) => {
    setMessages((prev) => [...prev, { id: crypto.randomUUID(), ...payload }]);
  }, []);

  const appendDetailLine = useCallback((text) => {
    if (!text) return;
    setDetailLines((prev) => [...prev, text]);
  }, []);

  const renderSteps = useCallback(
    (steps) => {
      if (!Array.isArray(steps) || steps.length === 0) return;
      const text = steps
        .map((item) => `${item.label}${item.duration ? `（${item.duration}）` : ""}`)
        .join("，");
      appendDetailLine(`执行过程：${text}`);
    },
    [appendDetailLine]
  );

  const renderQueue = useCallback(
    (queue) => {
      if (!queue) return;
      const position = queue.position ? `排队序号：${queue.position}` : "";
      const wait = queue.wait ? `等待：${queue.wait}` : "";
      const active = queue.active_at_start ? `并发占用：${queue.active_at_start}/${queue.limit || 5}` : "";
      const parts = [position, wait, active].filter(Boolean);
      if (parts.length) {
        const stableKey = [position, wait].filter(Boolean).join("|") || active;
        if (stableKey !== lastQueueKeyRef.current) {
          lastQueueKeyRef.current = stableKey;
          appendDetailLine(`排队信息：${parts.join("，")}`);
        }
      }
    },
    [appendDetailLine]
  );

  const renderProgress = useCallback(
    (items, fromIndex) => {
      if (!Array.isArray(items)) return fromIndex;
      for (let i = fromIndex; i < items.length; i += 1) {
        const item = items[i];
        const label = item.label || "";
        const detail = item.detail || "";
        if (label === "模型日志" && detail) {
          appendDetailLine(`模型日志：${detail}`);
          continue;
        }
        const text = detail ? `${label}：${detail}` : label;
        appendDetailLine(`进度：${text}`);
      }
      return items.length;
    },
    [appendDetailLine]
  );

  const appendFilesBubble = useCallback((title, rows) => {
    appendMessage({ type: "files", role: "assistant", title, rows });
  }, [appendMessage]);

  const renderFiles = useCallback((files) => {
    if (!Array.isArray(files) || !files.length) return;
    const rows = files.map((item, idx) => {
      const links = [];
      if (item.html) links.push({ text: "HTML", href: item.html });
      if (item.markdown) links.push({ text: "Markdown", href: item.markdown });
      if (item.geojson) links.push({ text: "GeoJSON", href: item.geojson });
      if (item.csv) links.push({ text: "CSV", href: item.csv });
      return { label: `人物${idx + 1} -`, links };
    }).filter((row) => row.links.length);
    if (rows.length) {
      appendFilesBubble("生成文件", rows);
    }
  }, [appendFilesBubble]);

  const renderMultiFiles = useCallback((multi) => {
    if (!multi || !multi.html) return;
    const links = [];
    if (multi.html) links.push({ text: "合并HTML", href: multi.html });
    if (multi.geojson) links.push({ text: "合并GeoJSON", href: multi.geojson });
    if (multi.csv) links.push({ text: "合并CSV", href: multi.csv });
    if (links.length) {
      appendFilesBubble("合并视图文件", [{ label: "", links }]);
    }
  }, [appendFilesBubble]);

  const renderResultJson = useCallback((data) => {
    try {
      const text = JSON.stringify(data, null, 2);
      appendDetailLine(`返回结果：\n${text}`);
    } catch (err) {
      appendDetailLine("返回结果解析失败");
    }
  }, [appendDetailLine]);

  const pollTask = useCallback((taskId) => {
    let progressIndex = 0;
    let lastStatus = "";
    let shownQueued = false;
    let shownRunning = false;
    const taskUrl = buildTaskUrl(taskId);
    if (!taskUrl) {
      appendMessage({ type: "text", role: "assistant", text: "当前接口不支持任务轮询，无法获取进度。" });
      return;
    }
    if (timerRef.current) {
      clearInterval(timerRef.current);
    }
    timerRef.current = setInterval(async () => {
      try {
        const resp = await fetch(taskUrl);
        if (!resp.ok) return;
        const data = await resp.json();
        if (!data || !data.ok) {
          appendMessage({ type: "text", role: "assistant", text: `任务查询失败：${data?.error || "未知错误"}` });
          clearInterval(timerRef.current);
          return;
        }
        progressIndex = renderProgress(data.progress, progressIndex);
        if (data.queue && data.queue.wait) {
          renderQueue(data.queue);
        }
        if (data.status && data.status !== lastStatus) {
          lastStatus = data.status;
          if (data.status === "running" && !shownRunning) {
            appendDetailLine("任务开始执行");
            shownRunning = true;
          }
          if (data.status === "queued" && !shownQueued) {
            appendDetailLine("任务排队中");
            shownQueued = true;
          }
        }
        if (data.status === "completed") {
          clearInterval(timerRef.current);
          const result = data.result || {};
          if (result.conclusion) {
            appendMessage({ type: "text", role: "assistant", text: `任务结论：${result.conclusion}` });
          }
          renderFiles(result.files);
          renderMultiFiles(result.multi);
          renderSteps(result.results?.flatMap((r) => r.steps || []));
          let path = "";
          if (result.multi && result.multi.html) {
            path = result.multi.html;
          } else if (result.files && result.files[0] && result.files[0].html) {
            path = result.files[0].html;
          }
          if (path) {
            const fileUrl = resolveFileUrl(path);
            const opened = window.open(fileUrl, "_blank");
            if (opened) {
              appendMessage({ type: "link", role: "assistant", text: "已生成并打开：", href: fileUrl });
            } else {
              appendMessage({ type: "link", role: "assistant", text: "已生成文件（浏览器可能阻止自动打开），点击打开：", href: fileUrl });
            }
          }
          renderResultJson(result);
        }
        if (data.status === "failed") {
          clearInterval(timerRef.current);
          appendMessage({ type: "text", role: "assistant", text: `任务失败：${data.error || "任务执行失败"}` });
          renderResultJson(data);
        }
      } catch (err) {
      }
    }, 1200);
  }, [appendDetailLine, appendMessage, renderFiles, renderMultiFiles, renderProgress, renderQueue, renderResultJson, renderSteps]);

  const openMap = useCallback(async (name) => {
    const text = String(name || "").trim();
    if (!text) return;
    lastQueueKeyRef.current = "";
    setDetailLines([]);
    appendMessage({ type: "text", role: "assistant", text: `正在生成 ${text} 的足迹地图...` });
    try {
      const resp = await fetch(buildApiUrl("/generate"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ person: text, text })
      });
      if (!resp.ok) {
        appendMessage({ type: "text", role: "assistant", text: `生成失败（HTTP ${resp.status}）。请确认服务可用：${API_ENDPOINT}` });
        return;
      }
      const data = await resp.json();
      if (!data || data.ok === false) {
        appendMessage({ type: "text", role: "assistant", text: `生成失败：${data?.error || "未知错误"}` });
        return;
      }
      if (data.task_id) {
        renderQueue(data.queue);
        pollTask(data.task_id, text);
        return;
      }
      const result = normalizeResultPayload(data);
      if (result?.conclusion) {
        appendMessage({ type: "text", role: "assistant", text: `任务结论：${result.conclusion}` });
      }
      renderFiles(result?.files || result?.items || result?.results);
      renderMultiFiles(result?.multi);
      const textReply = extractResultText(result);
      if (textReply) {
        appendMessage({ type: "text", role: "assistant", text: textReply });
      } else {
        appendMessage({ type: "text", role: "assistant", text: "已收到响应，但未包含可展示内容。" });
      }
      renderResultJson(result);
    } catch (err) {
      appendMessage({ type: "text", role: "assistant", text: `服务不可达，请确认接口可用：${API_ENDPOINT}` });
    }
  }, [appendMessage, pollTask, renderQueue]);

  const sendMessage = useCallback(() => {
    const value = inputValue.trim();
    if (!value) return;
    if (value.length > MAX_INPUT_LEN) {
      appendMessage({ type: "text", role: "assistant", text: `输入过长（最多 ${MAX_INPUT_LEN} 字符）` });
      return;
    }
    appendMessage({ type: "text", role: "user", text: value });
    setInputValue("");
    openMap(value);
  }, [appendMessage, inputValue, openMap]);

  const onSubmit = useCallback((event) => {
    event.preventDefault();
    sendMessage();
  }, [sendMessage]);

  const onNewChat = useCallback(() => {
    lastQueueKeyRef.current = "";
    setDetailLines([]);
    setMessages([
      {
        id: crypto.randomUUID(),
        type: "text",
        role: "assistant",
        text: "输入历史人物名称，我会检索相关事件并生成人物简介和足迹地图。"
      }
    ]);
  }, []);

  const onHistoryClick = useCallback((name) => {
    setInputValue(name);
    setTimeout(() => {
      openMap(name);
    }, 0);
  }, [openMap]);

  const renderMessage = useCallback((msg) => {
    const isUser = msg.role === "user";
    const avatarClass = `h-9 w-9 rounded-full flex items-center justify-center text-sm ${isUser ? "bg-slate-900 text-white" : "bg-slate-200 text-slate-700"}`;
    const avatarText = isUser ? "你" : "助手";
    const bubbleClass = "glass rounded-2xl px-4 py-3 text-sm text-slate-700 max-w-2xl";
    let content = null;
    if (msg.type === "link") {
      content = (
        <div>
          <span>{msg.text}</span>{" "}
          <a href={msg.href} target="_blank" rel="noopener" className="text-blue-600 underline">
            {msg.href}
          </a>
        </div>
      );
    } else if (msg.type === "files") {
      content = (
        <div>
          <div className="font-medium">{msg.title}</div>
          {msg.rows.map((row, idx) => (
            <div className="mt-1 text-xs text-slate-600" key={`${msg.id}-${idx}`}>
              {row.label ? <span>{row.label} </span> : null}
              {row.links.map((item, linkIdx) => (
                <span key={`${msg.id}-${idx}-${linkIdx}`}>
                  <a
                    href={resolveFileUrl(item.href)}
                    target="_blank"
                    rel="noopener"
                    className="text-blue-600 underline"
                    download
                  >
                    {item.text}
                  </a>
                  {linkIdx < row.links.length - 1 ? " | " : ""}
                </span>
              ))}
            </div>
          ))}
        </div>
      );
    } else {
      content = msg.text;
    }
    return (
      <div className="flex items-start gap-3" key={msg.id}>
        <div className={avatarClass}>{avatarText}</div>
        <div className={bubbleClass}>{content}</div>
      </div>
    );
  }, []);

  return (
    <div className="min-h-screen flex">
      <aside className="hidden lg:flex w-72 flex-col border-r bg-white/70">
        <div className="p-6">
          <div className="text-lg font-semibold text-slate-800">StoryMap</div>
          <div className="text-sm text-slate-500 mt-1">历史人物足迹地图</div>
        </div>
        <div className="px-6">
          <button onClick={onNewChat} className="w-full rounded-xl border px-4 py-2 text-sm google-outline">
            新建对话
          </button>
        </div>
        <div className="px-6 mt-6 text-xs text-slate-500">最近人物</div>
        <div className="px-4 pb-6 mt-3 space-y-2 text-sm text-slate-700">
          {historyItems.map((item) => (
            <button
              key={item}
              className="w-full text-left rounded-lg px-3 py-2 hover:bg-slate-100"
              onClick={() => onHistoryClick(item)}
            >
              {item}
            </button>
          ))}
        </div>
        <div className="mt-auto px-6 pb-6 text-xs text-slate-400">
          developed by 崔子橙（cuizicheng.1024@gmail.com)
        </div>
      </aside>
      <main className="flex-1 flex flex-col">
        <header className="px-6 py-5 border-b bg-white/70">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-xl font-semibold text-slate-800">故事地图</div>
              <div className="text-sm text-slate-500 mt-1">从空间视角，重新发现历史人物的人生轨迹</div>
            </div>
          </div>
        </header>
        <section className="flex-1 overflow-auto px-6 py-6 space-y-4">
          {messages.map(renderMessage)}
          {detailLines.length > 0 ? (
            <div className="flex items-start gap-3">
              <div className="h-9 w-9 rounded-full flex items-center justify-center text-sm bg-slate-200 text-slate-700">
                助手
              </div>
              <div className="glass rounded-2xl px-4 py-3 text-sm text-slate-700 max-w-2xl w-full">
                <details className="text-sm text-slate-700">
                  <summary className="cursor-pointer select-none text-slate-600">执行详情（可展开）</summary>
                  <div className="mt-2 space-y-1 text-xs text-slate-600 whitespace-pre-wrap max-h-64 overflow-auto pr-1">
                    {detailLines.map((line, idx) => (
                      <div key={`${idx}-${line.slice(0, 10)}`}>{line}</div>
                    ))}
                  </div>
                </details>
              </div>
            </div>
          ) : null}
          <div ref={chatEndRef}></div>
        </section>
        <form onSubmit={onSubmit} className="border-t bg-white/80 px-6 py-4">
          <div className="glass rounded-2xl p-3 flex items-center gap-3">
            <input
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
              className="flex-1 bg-transparent outline-none text-sm text-slate-700 placeholder-slate-400"
              placeholder="输入人物名称，例如：曹操"
            />
            <button type="submit" className="rounded-xl google-blue px-4 py-2 text-sm text-white">
              发送
            </button>
          </div>
          <div className="mt-2 text-xs text-slate-400">当前接口：{API_ENDPOINT}</div>
        </form>
      </main>
    </div>
  );
}
