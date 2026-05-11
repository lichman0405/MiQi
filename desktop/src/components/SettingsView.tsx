import { useState, useEffect, useRef } from "react";
import { useConfigRead } from "../lib/hooks";
import { request } from "../lib/ipc";
import {
  REDACTED_KEY,
  buildProviderConfigWriteUpdates,
  buildWorkspaceConfigWriteUpdates,
  initialProviderFormFromConfig,
  modelForProviderSwitch,
  providerCredentialsFromConfig,
  shouldInitializeProviderFromConfig,
} from "../lib/settings-contract";
import "./SettingsView.css";

type SettingsSection = "provider" | "workspace" | "mcp" | "approval" | "about";

const SECTIONS: { id: SettingsSection; label: string }[] = [
  { id: "provider", label: "Provider & Model" },
  { id: "workspace", label: "Workspace" },
  { id: "mcp", label: "MCP Servers" },
  { id: "approval", label: "Command Approval" },
  { id: "about", label: "About" },
];

export function SettingsView() {
  const [activeSection, setActiveSection] = useState<SettingsSection>("provider");

  return (
    <div className="settings-view">
      <aside className="settings-nav">
        <h2 className="settings-nav-title">Settings</h2>
        <ul className="settings-nav-list">
          {SECTIONS.map((s) => (
            <li key={s.id}>
              <button
                className={`settings-nav-item ${activeSection === s.id ? "settings-nav-item--active" : ""}`}
                onClick={() => setActiveSection(s.id)}
              >
                {s.label}
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <div className="settings-content">
        {activeSection === "provider" && <ProviderSection />}
        {activeSection === "workspace" && <WorkspaceSection />}
        {activeSection === "mcp" && <McpSection />}
        {activeSection === "approval" && <ApprovalSection />}
        {activeSection === "about" && <AboutSection />}
      </div>
    </div>
  );
}

function ProviderSection() {
  const { data: config, loading } = useConfigRead();
  const initializedFromConfig = useRef(false);

  const [provider, setProvider] = useState("anthropic");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiBase, setApiBase] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveResult, setSaveResult] = useState<{ success: boolean; message: string } | null>(null);
  const [apiKeyDirty, setApiKeyDirty] = useState(false);

  // Initialize provider/model once from config. Provider changes after this
  // are user intent and must not be pulled back by the saved model prefix.
  useEffect(() => {
    if (!shouldInitializeProviderFromConfig(config, initializedFromConfig.current)) return;
    const initial = initialProviderFormFromConfig(config);
    setProvider(initial.provider);
    setModel(initial.model);
    setApiKey(initial.apiKey);
    setApiBase(initial.apiBase);
    setApiKeyDirty(initial.apiKeyDirty);
    initializedFromConfig.current = true;
  }, [config]);

  if (loading) return <div className="settings-section"><p>Loading config...</p></div>;

  const handleProviderChange = (nextProvider: string) => {
    const oldProvider = provider;
    setProvider(nextProvider);
    setModel((currentModel) => modelForProviderSwitch(currentModel, oldProvider, nextProvider));
    const credentials = providerCredentialsFromConfig(config, nextProvider);
    setApiKey(credentials.apiKey);
    setApiBase(credentials.apiBase);
    setApiKeyDirty(credentials.apiKeyDirty);
    setTestResult(null);
    setSaveResult(null);
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await request<{ success: boolean; model?: string; preview?: string; error?: string }>(
        "config.testProvider",
        {
          provider,
          model: model || undefined,
          api_key: apiKeyDirty && apiKey !== REDACTED_KEY ? apiKey : undefined,
          api_base: apiBase || undefined,
        },
      );
      setTestResult({
        success: result.success,
        message: result.success ? `OK — ${result.preview ?? "connected"}` : (result.error ?? "Test failed"),
      });
    } catch (err) {
      setTestResult({ success: false, message: err instanceof Error ? err.message : String(err) });
    } finally {
      setTesting(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setSaveResult(null);
    try {
      const wroteApiKey = apiKeyDirty && apiKey !== REDACTED_KEY && apiKey !== "";
      const updates = buildProviderConfigWriteUpdates({
        provider,
        model,
        apiKey,
        apiBase,
        apiKeyDirty,
      });

      await request<{ success: boolean }>("config.write", { updates });
      setSaveResult({ success: true, message: "Saved" });
      if (wroteApiKey) setApiKey(REDACTED_KEY);
      setApiKeyDirty(false);
    } catch (err) {
      setSaveResult({ success: false, message: err instanceof Error ? err.message : String(err) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="settings-section">
      <h3 className="settings-section-title">Provider & Model</h3>

      <div className="settings-field">
        <label className="settings-label">Provider</label>
        <select value={provider} onChange={(e) => handleProviderChange(e.target.value)}>
          <option value="anthropic">Anthropic</option>
          <option value="openai">OpenAI</option>
          <option value="openrouter">OpenRouter</option>
          <option value="deepseek">DeepSeek</option>
          <option value="groq">Groq</option>
          <option value="zhipu">ZhipuAI</option>
          <option value="dashscope">DashScope</option>
          <option value="ollama">Ollama</option>
          <option value="custom">Custom</option>
        </select>
      </div>

      <div className="settings-field">
        <label className="settings-label">Model</label>
        <input type="text" value={model} onChange={(e) => setModel(e.target.value)} />
      </div>

      <div className="settings-field">
        <label className="settings-label">API Key</label>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => { setApiKey(e.target.value); setApiKeyDirty(true); }}
          placeholder="Enter API key..."
        />
        {!apiKeyDirty && apiKey === REDACTED_KEY && (
          <span className="settings-hint">Key is saved. Edit to replace.</span>
        )}
      </div>

      <div className="settings-field">
        <label className="settings-label">API Base <span className="settings-optional">(optional)</span></label>
        <input type="text" value={apiBase} onChange={(e) => setApiBase(e.target.value)} placeholder="https://api.example.com/v1" />
      </div>

      <div className="settings-actions">
        <button className="settings-btn settings-btn--primary" onClick={handleTest} disabled={testing}>
          {testing ? "Testing..." : "Test Connection"}
        </button>
        <button className="settings-btn" onClick={handleSave} disabled={saving}>
          {saving ? "Saving..." : "Save"}
        </button>
      </div>

      {testResult && (
        <div className={`settings-result ${testResult.success ? "settings-result--success" : "settings-result--error"}`}>
          {testResult.message}
        </div>
      )}
      {saveResult && (
        <div className={`settings-result ${saveResult.success ? "settings-result--success" : "settings-result--error"}`}>
          {saveResult.message}
        </div>
      )}
    </div>
  );
}

function WorkspaceSection() {
  const { data: config, loading } = useConfigRead();
  const defaults = config?.agents?.defaults;
  const tools = config?.tools;

  const [workspace, setWorkspace] = useState("");
  const [agentName, setAgentName] = useState("");
  const [maxTokens, setMaxTokens] = useState(8192);
  const [temperature, setTemperature] = useState(0.1);
  const [restrict, setRestrict] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveResult, setSaveResult] = useState<{ success: boolean; message: string } | null>(null);

  useEffect(() => {
    if (!defaults) return;
    setWorkspace(defaults.workspace ?? "");
    setAgentName(defaults.name ?? "miqi");
    setMaxTokens(defaults.maxTokens ?? defaults.max_tokens ?? 8192);
    setTemperature(defaults.temperature ?? 0.1);
    setRestrict(tools?.restrictToWorkspace ?? tools?.restrict_to_workspace ?? true);
  }, [defaults, tools]);

  if (loading) return <div className="settings-section"><p>Loading config...</p></div>;

  const handleSave = async () => {
    setSaving(true);
    setSaveResult(null);
    try {
      await request<{ success: boolean }>("config.write", {
        updates: buildWorkspaceConfigWriteUpdates({
          workspace,
          agentName,
          maxTokens,
          temperature,
          restrict,
        }),
      });
      setSaveResult({ success: true, message: "Saved" });
    } catch (err) {
      setSaveResult({ success: false, message: err instanceof Error ? err.message : String(err) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="settings-section">
      <h3 className="settings-section-title">Workspace</h3>

      <div className="settings-field">
        <label className="settings-label">Project Root</label>
        <div className="settings-input-row">
          <input type="text" value={workspace} onChange={(e) => setWorkspace(e.target.value)} />
        </div>
      </div>

      <div className="settings-field">
        <label className="settings-label">Agent Name</label>
        <input type="text" value={agentName} onChange={(e) => setAgentName(e.target.value)} />
      </div>

      <div className="settings-field">
        <label className="settings-label">Max Tokens</label>
        <input type="number" value={maxTokens} onChange={(e) => setMaxTokens(Number(e.target.value))} />
      </div>

      <div className="settings-field">
        <label className="settings-label">Temperature</label>
        <input type="number" step={0.1} value={temperature} onChange={(e) => setTemperature(Number(e.target.value))} />
      </div>

      <div className="settings-field">
        <label className="settings-label">Restrict to workspace</label>
        <label className="settings-toggle">
          <input type="checkbox" checked={restrict} onChange={(e) => setRestrict(e.target.checked)} />
          <span>{restrict ? "Enabled" : "Disabled"}</span>
        </label>
      </div>

      <div className="settings-actions">
        <button className="settings-btn settings-btn--primary" onClick={handleSave} disabled={saving}>
          {saving ? "Saving..." : "Save"}
        </button>
      </div>

      {saveResult && (
        <div className={`settings-result ${saveResult.success ? "settings-result--success" : "settings-result--error"}`}>
          {saveResult.message}
        </div>
      )}
    </div>
  );
}

function McpSection() {
  return (
    <div className="settings-section">
      <h3 className="settings-section-title">MCP Servers</h3>
      <p className="settings-description">
        Configure Model Context Protocol servers to extend the agent's capabilities.
      </p>

      <div className="settings-mcp-empty">
        <span>MCP server management is available via CLI: miqi config mcp list</span>
      </div>
    </div>
  );
}

function ApprovalSection() {
  return (
    <div className="settings-section">
      <h3 className="settings-section-title">Command Approval</h3>

      <div className="settings-field">
        <label className="settings-label">Command Approval Mode</label>
        <select defaultValue="manual">
          <option value="manual">Manual — require approval for each dangerous command</option>
          <option value="session">Session — approve all dangerous commands for this session</option>
        </select>
        <span className="settings-description">
          Dangerous shell commands always require explicit approval in the desktop app.
        </span>
      </div>

      <div className="settings-field">
        <label className="settings-label">Approval Timeout (seconds)</label>
        <input type="number" defaultValue={60} />
      </div>

      <div className="settings-field">
        <label className="settings-label">Permanent Allowlist</label>
        <textarea defaultValue="" placeholder="One command pattern per line..." rows={3} />
      </div>
    </div>
  );
}

function AboutSection() {
  return (
    <div className="settings-section">
      <h3 className="settings-section-title">About MiQi</h3>
      <dl className="settings-details">
        <dt>Version</dt>
        <dd>0.1.0</dd>
        <dt>Runtime</dt>
        <dd>Python sidecar (miqi desktop-backend --stdio)</dd>
        <dt>IPC</dt>
        <dd>JSON-RPC 2.0 over stdio</dd>
        <dt>Shell</dt>
        <dd>Tauri 2</dd>
      </dl>
    </div>
  );
}
