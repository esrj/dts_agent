# Local LM Server — API 使用說明

本機本地模型服務的接法。任何專案（Phase 2 及之後）都照這份接即可。

---

## 0. 連線資訊（合約）

| 項目 | 值 |
| --- | --- |
| Base URL（OpenAI 相容） | `http://localhost:11434/v1` |
| 端點 | `/chat/completions`、`/completions`、`/embeddings` |
| 模型名稱 | `qwen3.6-8k` |
| 認證 | **無**。用 OpenAI SDK 時 `api_key` 填任意非空字串（如 `"ollama"`） |
| Context | 8192 token（輸入＋輸出共用） |
| 原生 Ollama 端點 | `http://localhost:11434/api/generate`、`/api/chat`、`/api/tags` |

> 為什麼建議用 OpenAI 相容端點：未來底層換 vLLM / llama.cpp 也不用改專案程式。

---

## 1. 先確認服務在跑（30 秒）

```bash
curl -s http://localhost:11434/api/tags        # 應回 JSON，列出 qwen3.6-8k
ollama ps                                       # 應顯示 qwen3.6-8k、100% GPU、CONTEXT 8192
```
沒回應就執行：`./restart_server.sh`（見 README 的「如何運行」）。

---

## 2. 最快測試（curl）

```bash
curl -s http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6-8k",
    "messages": [{"role": "user", "content": "用一句話說明什麼是 HTTP。"}],
    "temperature": 0.7,
    "reasoning_effort": "none"
  }'
```
回傳結構與 OpenAI 完全相同，答案在 `choices[0].message.content`。

---

## 3. Python（建議：官方 openai SDK）

```bash
pip install openai
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")

resp = client.chat.completions.create(
    model="qwen3.6-8k",
    messages=[
        {"role": "system", "content": "你是簡潔的助理，用繁體中文回答。"},
        {"role": "user", "content": "什麼是向量資料庫？"},
    ],
    temperature=0.7,
    extra_body={"reasoning_effort": "none"},   # 關閉 thinking → 純輸出、低延遲
)
print(resp.choices[0].message.content)
```

> `reasoning_effort` 不是 OpenAI 標準欄位，openai SDK 要放在 `extra_body` 裡傳。

---

## 4. Python（不裝 SDK，純 requests）

```python
import requests

r = requests.post("http://localhost:11434/v1/chat/completions", json={
    "model": "qwen3.6-8k",
    "messages": [{"role": "user", "content": "Hello"}],
    "temperature": 0.7,
    "reasoning_effort": "none",
}, timeout=120)
print(r.json()["choices"][0]["message"]["content"])
```

---

## 5. Node.js / TypeScript（官方 openai SDK）

```bash
npm install openai
```

```javascript
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://localhost:11434/v1", apiKey: "ollama" });

const resp = await client.chat.completions.create({
  model: "qwen3.6-8k",
  messages: [{ role: "user", content: "什麼是 REST API？" }],
  temperature: 0.7,
  // @ts-ignore 非標準欄位
  reasoning_effort: "none",
});
console.log(resp.choices[0].message.content);
```

或純 fetch：
```javascript
const res = await fetch("http://localhost:11434/v1/chat/completions", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    model: "qwen3.6-8k",
    messages: [{ role: "user", content: "Hello" }],
    reasoning_effort: "none",
  }),
});
const data = await res.json();
console.log(data.choices[0].message.content);
```

---

## 6. 串流輸出（streaming，逐 token）

**Python SDK：**
```python
stream = client.chat.completions.create(
    model="qwen3.6-8k",
    messages=[{"role": "user", "content": "從 1 數到 10"}],
    stream=True,
    extra_body={"reasoning_effort": "none"},
)
for chunk in stream:
    delta = chunk.choices[0].delta.content or ""
    print(delta, end="", flush=True)
print()
```

**curl（SSE）：**
```bash
curl -N http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.6-8k","stream":true,"reasoning_effort":"none","messages":[{"role":"user","content":"從 1 數到 10"}]}'
```
> 串流以 `data: {...}` 多行回傳，最後一行為 `data: [DONE]`。
> 注意：併發競爭下首 token 可能延遲到數十秒，client timeout 請設寬鬆（≥60s）。

---

## 7. 取得乾淨 JSON（重要：要「約束＋prompt」雙管）

`response_format` 單獨**不保證**乾淨——prompt 沒強調時模型會吐 ```json 圍欄。已驗證可靠配方：

```python
import json, re

resp = client.chat.completions.create(
    model="qwen3.6-8k",
    messages=[{"role": "user",
               "content": "Return ONLY a JSON object (no markdown, no code fences) "
                          "with fields city and country for Tokyo."}],
    response_format={"type": "json_object"},
    temperature=0.7,
    extra_body={"reasoning_effort": "none"},
)
raw = resp.choices[0].message.content
clean = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()  # 防禦性去圍欄
data = json.loads(clean)
print(data)        # {'city': 'Tokyo', 'country': 'Japan'}
```

重點：① prompt 明寫「ONLY JSON, no markdown」② 帶 `response_format` ③ 仍防禦性 strip 圍欄再 parse。

---

## 8. Thinking（推理）開關

此模型預設**開啟 thinking**（會先推理再回答）。

| 需求 | 做法 |
| --- | --- |
| 要推理能力（agentic / 難題） | 不傳 `reasoning_effort`（預設開）。答案在 `message.content`，推理在 `message.reasoning`（非標準欄，可忽略） |
| 要純輸出 / 低延遲 / 乾淨 JSON | 傳 `reasoning_effort: "none"` |

> 原生 `/api/chat`、`/api/generate` 端點則是用 `"think": false` 來關（OpenAI 端點不吃 `think`，只吃 `reasoning_effort`）。

---

## 9. 常用參數參考

| 參數 | 說明 | 建議 |
| --- | --- | --- |
| `temperature` | 隨機性 | 推理用 1.0；穩定/抽取用 0.7（勿用 0）|
| `top_p` | 核採樣 | 思考 0.95 / 非思考 0.80 |
| `max_tokens` | 輸出上限 | 視需要；注意與 8192 context 共用 |
| `stop` | 停止字串陣列 | 選用 |
| `seed` | 固定亂數 | 要可重現時用（同 seed+參數 → 同輸出）|
| `response_format` | `{"type":"json_object"}` | 配合 §7 |
| `reasoning_effort` | `"none"` 關 thinking | 見 §8 |
| `stream` | 串流 | 見 §6 |

被忽略的參數：`tool_choice, logit_bias, user, n, logprobs, top_logprobs`。

---

## 10. 常見坑（務必知道）

1. **超長輸入會被靜默截斷**：輸入超過 8192 token 時，伺服器砍掉**前段**、只看後半，不報錯（`done_reason:"length"`）。請自行控制 token 數，或檢查回應的 `done_reason` / `prompt_eval_count`。
2. **單請求 serving**（`num_parallel=1`）：併發請求會排隊，不會真正平行。多專案同時打 → 後者等前者。client 端請設寬 timeout 或自行序列化。
3. **JSON 圍欄**：見 §7。
4. **冷載入**：重開機或閒置 2 小時後，第一個請求要等 ~9.4s 載入 23GB；之後就快。
5. **記憶體**：跑模型時別開太多吃記憶體的 App（23GB 模型放 32GB 餘裕低）。

---

## 11. 原生 Ollama 端點（進階／需要 `num_ctx`、`think` 等非 OpenAI 欄位時）

```bash
# 單次生成
curl http://localhost:11434/api/generate -d '{
  "model": "qwen3.6-8k", "prompt": "你好", "stream": false, "think": false
}'

# 對話（可帶 options 細調）
curl http://localhost:11434/api/chat -d '{
  "model": "qwen3.6-8k",
  "messages": [{"role":"user","content":"你好"}],
  "stream": false, "think": false,
  "options": {"temperature": 0.7, "num_ctx": 8192}
}'
```
> 原生端點的回應結構與 OpenAI 不同（`response` / `message.content`）。Phase 2 專案建議優先用 §0 的 OpenAI 相容端點以保持可攜性。
