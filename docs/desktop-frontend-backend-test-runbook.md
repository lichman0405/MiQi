# Desktop Frontend + Backend Test Runbook

This runbook verifies the real MiQi desktop path: React frontend in the Tauri window, Python desktop backend as a sidecar, and the agent backend calling a real LLM plus built-in web and paper search tools.

MCP servers are intentionally out of scope for this test. Do not debug MCP until the LLM, web search, paper search, and Tauri sidecar path are working.

## 1. Set Up The Python Environment

Run from the repository root:

```powershell
Set-Location 'C:\Users\lishi\code\MiQi'
uv sync --extra dev
```

## 2. Configure The LLM Provider

Use one real provider and one real model. This example uses OpenRouter.

```powershell
Set-Location 'C:\Users\lishi\code\MiQi'
$env:LLM_API_KEY = 'YOUR_OPENROUTER_API_KEY'
uv run miqi config provider openrouter --api-key $env:LLM_API_KEY
notepad "$HOME\.miqi\config.json"
```

Make sure the config contains a valid default model and provider key:

```json
{
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5"
    }
  },
  "providers": {
    "openrouter": {
      "apiKey": "YOUR_OPENROUTER_API_KEY"
    }
  }
}
```

For another provider, replace both the provider name and model. Examples:

```powershell
uv run miqi config provider deepseek --api-key 'YOUR_DEEPSEEK_API_KEY'
uv run miqi config provider anthropic --api-key 'YOUR_ANTHROPIC_API_KEY'
uv run miqi config provider openai --api-key 'YOUR_OPENAI_API_KEY'
```

## 3. Configure Web Search And Paper Search

Edit the same config file:

```powershell
notepad "$HOME\.miqi\config.json"
```

Merge this `tools` section into the config. Use a real Brave Search API key for stable web search. Semantic Scholar is optional because paper search can fall back to arXiv.

```json
{
  "tools": {
    "web": {
      "search": {
        "provider": "brave",
        "apiKey": "YOUR_BRAVE_SEARCH_API_KEY",
        "maxResults": 5
      },
      "fetch": {
        "provider": "builtin"
      }
    },
    "papers": {
      "provider": "hybrid",
      "semanticScholarApiKey": "YOUR_SEMANTIC_SCHOLAR_API_KEY_OR_EMPTY",
      "defaultLimit": 5,
      "maxLimit": 10
    }
  }
}
```

Do not add MCP servers for this test.

## 4. Verify The Agent Backend Can Call The LLM

Run this before opening the desktop UI:

```powershell
Set-Location 'C:\Users\lishi\code\MiQi'
uv run miqi agent -m "Reply with OK only."
```

Pass condition: the response is `OK` or a clearly successful equivalent.

If this fails, stop here and fix the provider, model, API key, or network access. Do not start the frontend yet.

## 5. Verify Web Search And Paper Search From The Agent

Run:

```powershell
uv run miqi agent -m "Use web_search to find one current AI news item, then use paper_search to find papers about transformer sparse attention. Summarize one web result and one paper result in Chinese."
```

Pass conditions:

- The response includes a web search result summary.
- The response includes at least one paper title or arXiv/Semantic Scholar result.
- There are no provider authentication errors.
- There are no tool configuration errors.

## 6. Launch The Real Desktop Frontend + Backend Path

Do not use `npm run dev` for this test. Plain Vite uses MockTransport and does not connect to the real Python backend.

Run:

```powershell
Set-Location 'C:\Users\lishi\code\MiQi\desktop'
npm install
npm run sidecar:dev
npm run tauri dev
```

The expected path is:

- Tauri opens the native desktop window.
- Tauri launches the Python sidecar automatically.
- The React frontend talks to the sidecar over stdio JSON-RPC.
- The bottom-right connection indicator shows `Connected`, not `Mock mode`.

## 7. Test In The Frontend

In the desktop window, send this prompt:

```text
Search for one current AI news item, then search for one paper about sparse attention. Summarize the sources in Chinese. Do not use MCP.
```

Pass conditions:

- The chat returns a real assistant response.
- The Activity or tool area shows real backend activity.
- `web_search` returns a relevant web result.
- `paper_search` returns a relevant paper result.
- No MCP-related error blocks the run.

## Quick Pass/Fail Chain

Use this short sequence when you only need a fast gate:

```powershell
Set-Location 'C:\Users\lishi\code\MiQi'
uv run miqi agent -m "Reply with OK only."
uv run miqi agent -m "Use web_search to search AI news, then use paper_search to search sparse attention papers."

Set-Location 'C:\Users\lishi\code\MiQi\desktop'
npm run sidecar:dev
npm run tauri dev
```

If the two CLI agent checks pass, the backend LLM and built-in search tools are usable. If the Tauri window opens and shows `Connected`, the real frontend-backend desktop path is usable.