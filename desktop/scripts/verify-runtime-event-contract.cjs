const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

function loadTsModule(sourcePath, requireStub = () => {
  throw new Error("No imports are expected in this contract probe");
}) {
  const source = fs.readFileSync(sourcePath, "utf8");
  const transpiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2021,
    },
    fileName: sourcePath,
  }).outputText;
  const moduleStub = { exports: {} };

  vm.runInNewContext(transpiled, {
    require: requireStub,
    module: moduleStub,
    exports: moduleStub.exports,
  });

  return moduleStub.exports;
}

function assertNoDotPathKeys(value, pathPrefix = "") {
  if (!value || typeof value !== "object" || Array.isArray(value)) return;
  for (const [key, child] of Object.entries(value)) {
    const pathText = pathPrefix ? `${pathPrefix}.${key}` : key;
    assert.equal(key.includes("."), false, `dot-path key found at ${pathText}`);
    assertNoDotPathKeys(child, pathText);
  }
}

const subscribedHandlers = [];
const rpcCalls = [];

function chatStateRequireStub(specifier) {
  if (specifier === "./ipc") {
    return {
      request: async (method, params) => {
        rpcCalls.push({ method, params });
        return {
          key: params?.key ?? "desktop:probe",
          title: params?.title ?? "Probe",
          archived: false,
          deleted: true,
          sessions: [],
          count: 0,
        };
      },
      subscribe: (handler) => {
        subscribedHandlers.push(handler);
        return () => {
          const index = subscribedHandlers.indexOf(handler);
          if (index !== -1) subscribedHandlers.splice(index, 1);
        };
      },
    };
  }
  throw new Error(`Unexpected import in contract probe: ${specifier}`);
}

const {
  INITIAL_CHAT_STATE,
  archiveSession,
  chatReducer,
  deleteSession,
  renameSession,
  runtimeEventFromJsonRpcEvent,
  searchSessions,
  subscribeRuntimeEvents,
  unarchiveSession,
} = loadTsModule(
  path.join(__dirname, "..", "src", "lib", "chat-state.ts"),
  chatStateRequireStub,
);

const methodStyleRunStarted = runtimeEventFromJsonRpcEvent({
  jsonrpc: "2.0",
  method: "RunStarted",
  params: {
    execution_id: "exec-method-style",
    session_key: "desktop:default",
  },
});

assert.equal(methodStyleRunStarted.type, "RunStarted");
assert.equal(methodStyleRunStarted.execution_id, "exec-method-style");
assert.equal(methodStyleRunStarted.session_key, "desktop:default");

const legacyEnvelopeDelta = runtimeEventFromJsonRpcEvent({
  jsonrpc: "2.0",
  method: "runtime_event",
  params: {
    type: "MessageDelta",
    execution_id: "exec-method-style",
    delta: "hello",
  },
});

assert.equal(legacyEnvelopeDelta.type, "MessageDelta");
assert.equal(legacyEnvelopeDelta.delta, "hello");

assert.equal(
  runtimeEventFromJsonRpcEvent({
    jsonrpc: "2.0",
    method: "SessionChanged",
    params: { key: "desktop:default" },
  }),
  null,
);

let delivered = null;
const unsubscribe = subscribeRuntimeEvents((event) => {
  delivered = event;
});

assert.equal(subscribedHandlers.length, 1);
subscribedHandlers[0]({
  jsonrpc: "2.0",
  method: "MessageDelta",
  params: {
    execution_id: "exec-method-style",
    delta: " streamed",
  },
});

assert.equal(delivered.type, "MessageDelta");
assert.equal(delivered.delta, " streamed");

unsubscribe();
assert.equal(subscribedHandlers.length, 0);

const {
  REDACTED_KEY,
  buildProviderConfigWriteUpdates,
  buildWorkspaceConfigWriteUpdates,
  initialProviderFormFromConfig,
  modelForProviderSwitch,
  providerCredentialsFromConfig,
  shouldInitializeProviderFromConfig,
} = loadTsModule(path.join(__dirname, "..", "src", "lib", "settings-contract.ts"));

const sampleConfig = {
  agents: {
    defaults: {
      model: "anthropic/claude-sonnet-4-5",
      workspace: "~/projects/demo",
      maxTokens: 8192,
      temperature: 0.1,
    },
  },
  providers: {
    anthropic: { apiKey: "sk-anthropic", apiBase: "" },
    openai: { api_key: "sk-openai", api_base: "https://api.openai.test/v1" },
  },
  tools: { restrictToWorkspace: false },
};

const initialProvider = initialProviderFormFromConfig(sampleConfig);
assert.equal(initialProvider.provider, "anthropic");
assert.equal(initialProvider.model, "anthropic/claude-sonnet-4-5");
assert.equal(initialProvider.apiKey, REDACTED_KEY);
assert.equal(initialProvider.apiKeyDirty, false);

let selectedProvider = initialProvider.provider;
let initializedFromConfig = true;
selectedProvider = "openai";
if (shouldInitializeProviderFromConfig(sampleConfig, initializedFromConfig)) {
  selectedProvider = initialProviderFormFromConfig(sampleConfig).provider;
}
assert.equal(selectedProvider, "openai");

const openaiCredentials = providerCredentialsFromConfig(sampleConfig, selectedProvider);
assert.equal(openaiCredentials.apiKey, REDACTED_KEY);
assert.equal(openaiCredentials.apiBase, "https://api.openai.test/v1");
assert.equal(openaiCredentials.apiKeyDirty, false);

const switchedModel = modelForProviderSwitch(
  "anthropic/claude-sonnet-4-5",
  "anthropic",
  "openai",
);
assert.equal(switchedModel, "claude-sonnet-4-5");

const providerUpdates = buildProviderConfigWriteUpdates({
  provider: "openai",
  model: switchedModel,
  apiKey: "sk-new-openai",
  apiBase: "https://api.openai.test/v1",
  apiKeyDirty: true,
});
assertNoDotPathKeys(providerUpdates);
assert.equal(providerUpdates.agents.defaults.model, "openai/claude-sonnet-4-5");
assert.equal(providerUpdates.providers.openai.api_key, "sk-new-openai");
assert.equal(providerUpdates.providers.openai.api_base, "https://api.openai.test/v1");

const alreadyPrefixedProviderUpdates = buildProviderConfigWriteUpdates({
  provider: "openai",
  model: "openai/gpt-4o",
  apiKey: REDACTED_KEY,
  apiBase: "",
  apiKeyDirty: false,
});
assert.equal(alreadyPrefixedProviderUpdates.agents.defaults.model, "openai/gpt-4o");

const unprefixedProviderUpdates = buildProviderConfigWriteUpdates({
  provider: "openai",
  model: "gpt-4o",
  apiKey: REDACTED_KEY,
  apiBase: "",
  apiKeyDirty: false,
});
assert.equal(unprefixedProviderUpdates.agents.defaults.model, "openai/gpt-4o");

const oldProviderPrefixUpdates = buildProviderConfigWriteUpdates({
  provider: "openai",
  model: "anthropic/claude-sonnet-4-5",
  apiKey: REDACTED_KEY,
  apiBase: "",
  apiKeyDirty: false,
});
assert.equal(oldProviderPrefixUpdates.agents.defaults.model, "openai/claude-sonnet-4-5");
assert.notEqual(
  oldProviderPrefixUpdates.agents.defaults.model,
  "openai/anthropic/claude-sonnet-4-5",
);

const workspaceUpdates = buildWorkspaceConfigWriteUpdates({
  workspace: "~/projects/demo",
  agentName: "miqi",
  maxTokens: 4096,
  temperature: 0.7,
  restrict: true,
});
assertNoDotPathKeys(workspaceUpdates);
assert.equal(workspaceUpdates.agents.defaults.workspace, "~/projects/demo");
assert.equal(workspaceUpdates.agents.defaults.max_tokens, 4096);
assert.equal(workspaceUpdates.tools.restrict_to_workspace, true);
assert.equal(workspaceUpdates.agents.defaults.restrict_to_workspace, undefined);

const {
  canSwitchSession,
  shouldApplySessionLoadResponse,
  shouldClearChatAfterSessionDelete,
} = loadTsModule(path.join(__dirname, "..", "src", "lib", "session-ux.ts"));

const {
  contextStatus,
  indexWorkspace,
  listContextBootstrap,
  listContextSkills,
  listPinnedWorkspaceFiles,
  listRecentWorkspaceFiles,
  openWorkspace,
  pinWorkspaceFile,
  previewWorkspaceFile,
  readContextBootstrap,
  shouldApplyWorkspacePreviewResponse,
  unpinWorkspaceFile,
} = loadTsModule(
  path.join(__dirname, "..", "src", "lib", "workspace-state.ts"),
  chatStateRequireStub,
);

const {
  addCronJob,
  appendTodayMemory,
  deleteCronJob,
  deleteMemoryLesson,
  deleteMemorySnapshotItem,
  getHeartbeatStatus,
  getMcpStatus,
  getMemoryStatus,
  learnMemoryLesson,
  listCronJobs,
  listMemoryLessons,
  listMemorySnapshot,
  rememberMemory,
  searchMemory,
  setMemoryLessonEnabled,
  updateCronJob,
  updateHeartbeat,
  updateMemory,
} = loadTsModule(
  path.join(__dirname, "..", "src", "lib", "ops-state.ts"),
  chatStateRequireStub,
);

async function verifySessionContracts() {
  rpcCalls.length = 0;

  await searchSessions("api", true);
  await renameSession("desktop:s1", "Renamed session");
  await archiveSession("desktop:s1");
  await unarchiveSession("desktop:s1");
  await deleteSession("desktop:s1");

  assert.deepEqual(JSON.parse(JSON.stringify(rpcCalls)), [
    { method: "session.search", params: { query: "api", include_archived: true } },
    { method: "session.rename", params: { key: "desktop:s1", title: "Renamed session" } },
    { method: "session.archive", params: { key: "desktop:s1" } },
    { method: "session.unarchive", params: { key: "desktop:s1" } },
    { method: "session.delete", params: { key: "desktop:s1" } },
  ]);

  assert.equal(
    shouldApplySessionLoadResponse(1, 2, "desktop:old", "desktop:old"),
    false,
  );
  assert.equal(
    shouldApplySessionLoadResponse(2, 2, "desktop:new", "desktop:new"),
    true,
  );
  assert.equal(
    shouldApplySessionLoadResponse(2, 2, "desktop:new", "desktop:old"),
    false,
  );
  assert.equal(
    shouldApplySessionLoadResponse(5, 6, "desktop:removed", "desktop:removed"),
    false,
  );

  const sessionBState = {
    ...INITIAL_CHAT_STATE,
    sessionKey: "desktop:B",
    sessionTitle: "B",
  };
  const ignoredRunStarted = chatReducer(sessionBState, {
    type: "EVENT",
    event: {
      type: "RunStarted",
      execution_id: "exec-A",
      session_key: "desktop:A",
    },
  });
  assert.equal(ignoredRunStarted.executionId, null);
  assert.equal(ignoredRunStarted.executionStatus, "idle");
  assert.equal(ignoredRunStarted.messages.length, 0);

  const sessionAState = {
    ...INITIAL_CHAT_STATE,
    sessionKey: "desktop:A",
    sessionTitle: "A",
  };
  const acceptedRunStarted = chatReducer(sessionAState, {
    type: "EVENT",
    event: {
      type: "RunStarted",
      execution_id: "exec-A",
      session_key: "desktop:A",
    },
  });
  assert.equal(acceptedRunStarted.executionId, "exec-A");
  assert.equal(acceptedRunStarted.executionStatus, "running");
  assert.equal(acceptedRunStarted.messages.length, 1);
  assert.equal(acceptedRunStarted.messages[0].id, "asst-exec-A");

  const wrongSessionCompleted = chatReducer(acceptedRunStarted, {
    type: "EVENT",
    event: {
      type: "RunCompleted",
      execution_id: "exec-A",
      session_key: "desktop:B",
    },
  });
  assert.equal(wrongSessionCompleted.executionId, "exec-A");
  assert.equal(wrongSessionCompleted.executionStatus, "running");

  const wrongSessionCancelled = chatReducer(acceptedRunStarted, {
    type: "EVENT",
    event: {
      type: "RunCancelled",
      execution_id: "exec-A",
      session_key: "desktop:B",
    },
  });
  assert.equal(wrongSessionCancelled.executionId, "exec-A");
  assert.equal(wrongSessionCancelled.executionStatus, "running");

  const startingState = chatReducer(sessionAState, {
    type: "ADD_USER_MESSAGE",
    id: "user-1",
    content: "hello",
  });
  assert.equal(startingState.executionStatus, "starting");
  assert.equal(canSwitchSession(startingState.executionStatus), false);
  assert.equal(startingState.executionId, null);

  const failedSendState = chatReducer(startingState, {
    type: "SET_ERROR",
    message: "chat.send failed",
  });
  assert.equal(failedSendState.executionStatus, "failed");
  assert.equal(failedSendState.executionId, null);
  assert.equal(failedSendState.error, "chat.send failed");

  const clearedStarting = chatReducer(startingState, { type: "CLEAR_SESSION" });
  assert.equal(clearedStarting.sessionKey, null);
  assert.equal(clearedStarting.executionStatus, "idle");
  assert.equal(clearedStarting.executionId, null);
  assert.equal(clearedStarting.messages.length, 0);

  assert.equal(shouldClearChatAfterSessionDelete("desktop:s1", "desktop:s1"), true);
  const activeState = {
    ...INITIAL_CHAT_STATE,
    sessionKey: "desktop:s1",
    sessionTitle: "Active",
    messages: [{ id: "m1", role: "user", content: "hello", status: "complete", toolCalls: [] }],
  };
  const cleared = chatReducer(activeState, { type: "CLEAR_SESSION" });
  assert.equal(cleared.sessionKey, null);
  assert.equal(cleared.messages.length, 0);
}

async function verifyWorkspaceContracts() {
  rpcCalls.length = 0;

  await openWorkspace("C:/work/miqi");
  await indexWorkspace({ depth: 4 });
  await previewWorkspaceFile("desktop/src/App.tsx");
  await pinWorkspaceFile("desktop/src/App.tsx");
  await unpinWorkspaceFile("desktop/src/App.tsx");
  await listPinnedWorkspaceFiles();
  await listRecentWorkspaceFiles(10);
  await contextStatus();
  await listContextBootstrap();
  await readContextBootstrap("TOOLS.md");
  await listContextSkills();

  assert.deepEqual(JSON.parse(JSON.stringify(rpcCalls)), [
    { method: "workspace.open", params: { path: "C:/work/miqi" } },
    { method: "workspace.index", params: { depth: 4 } },
    { method: "workspace.preview", params: { path: "desktop/src/App.tsx" } },
    { method: "workspace.pinFile", params: { path: "desktop/src/App.tsx" } },
    { method: "workspace.unpinFile", params: { path: "desktop/src/App.tsx" } },
    { method: "workspace.listPinned" },
    { method: "workspace.listRecent", params: { limit: 10 } },
    { method: "context.status" },
    { method: "context.listBootstrap" },
    { method: "context.readBootstrap", params: { name: "TOOLS.md" } },
    { method: "context.listSkills" },
  ]);

  assert.equal(
    shouldApplyWorkspacePreviewResponse(1, 2, "a.txt", "a.txt", "a.txt"),
    false,
  );
  assert.equal(
    shouldApplyWorkspacePreviewResponse(2, 2, "a.txt", "b.txt", "a.txt"),
    false,
  );
  assert.equal(
    shouldApplyWorkspacePreviewResponse(2, 2, "a.txt", "a.txt", "b.txt"),
    false,
  );
  assert.equal(
    shouldApplyWorkspacePreviewResponse(2, 2, "a.txt", "a.txt", "a.txt"),
    true,
  );
}

async function verifyOpsContracts() {
  rpcCalls.length = 0;

  await getMemoryStatus();
  await searchMemory("latency", 5);
  await updateMemory({ text: "remember via update", action: "remember" });
  await rememberMemory("remember direct");
  await appendTodayMemory("today note");
  await learnMemoryLesson({
    trigger: "slow query",
    betterAction: "add an index",
    badAction: "scan everything",
  });
  await listMemorySnapshot(10);
  await listMemoryLessons(true, 10);
  await setMemoryLessonEnabled("lesson-1", false);
  await deleteMemoryLesson("lesson-1");
  await deleteMemorySnapshotItem("mem-1");
  await listCronJobs(true);
  await addCronJob({
    name: "daily check",
    message: "Summarize status",
    schedule: { kind: "every", every_ms: 60000 },
  });
  await updateCronJob("cron-1", false);
  await deleteCronJob("cron-1");
  await getHeartbeatStatus();
  await updateHeartbeat({ enabled: false, intervalSeconds: 900 });
  await getMcpStatus();

  assert.deepEqual(JSON.parse(JSON.stringify(rpcCalls)), [
    { method: "memory.status" },
    { method: "memory.search", params: { query: "latency", limit: 5 } },
    { method: "memory.update", params: { text: "remember via update", action: "remember" } },
    { method: "memory.remember", params: { text: "remember direct" } },
    { method: "memory.appendToday", params: { content: "today note" } },
    {
      method: "memory.learnLesson",
      params: {
        trigger: "slow query",
        better_action: "add an index",
        bad_action: "scan everything",
      },
    },
    { method: "memory.listSnapshot", params: { limit: 10 } },
    { method: "memory.listLessons", params: { include_disabled: true, limit: 10 } },
    { method: "memory.setLessonEnabled", params: { lesson_id: "lesson-1", enabled: false } },
    { method: "memory.deleteLesson", params: { lesson_id: "lesson-1" } },
    { method: "memory.deleteSnapshotItem", params: { item_id: "mem-1" } },
    { method: "cron.list", params: { include_disabled: true } },
    {
      method: "cron.add",
      params: {
        name: "daily check",
        message: "Summarize status",
        schedule: { kind: "every", every_ms: 60000 },
      },
    },
    { method: "cron.update", params: { job_id: "cron-1", enabled: false } },
    { method: "cron.delete", params: { job_id: "cron-1" } },
    { method: "heartbeat.status" },
    { method: "heartbeat.update", params: { enabled: false, interval_seconds: 900 } },
    { method: "mcp.status" },
  ]);
}

function verifyToolsPanelRefreshContract() {
  const source = fs.readFileSync(
    path.join(__dirname, "..", "src", "components", "Inspector.tsx"),
    "utf8",
  );

  assert.match(source, /function ToolsPanel\(\)/);
  assert.match(source, /const refreshTools = \(\) => \{/);
  assert.match(source, /toolData\.refresh\(\);/);
  assert.match(source, /mcpData\.refresh\(\);/);
  assert.match(source, /<button type="button" onClick=\{refreshTools\}>Refresh<\/button>/);
}

function verifyMemoryHookRefreshContract() {
  const source = fs.readFileSync(
    path.join(__dirname, "..", "src", "lib", "hooks.ts"),
    "utf8",
  );

  assert.match(
    source,
    /export function useMemoryStatus\(\) \{\s*const fetch = useIpcFetch<MemoryStatusResult>\("memory\.status"\);\s*useIpcRefreshOnEvent\(fetch\.refresh, "MemoryChanged"\);\s*return fetch;\s*\}/,
  );
  assert.match(
    source,
    /export function useMemorySnapshot\(limit = 50\) \{\s*const fetch = useIpcFetch<SnapshotListResponse>\("memory\.listSnapshot", \{ limit \}\);\s*useIpcRefreshOnEvent\(fetch\.refresh, "MemoryChanged"\);\s*return fetch;\s*\}/,
  );
  assert.match(
    source,
    /export function useMemoryLessons\(includeDisabled = true, limit = 50\) \{\s*const fetch = useIpcFetch<LessonListResponse>\("memory\.listLessons", \{\s*include_disabled: includeDisabled,\s*limit,\s*\}\);\s*useIpcRefreshOnEvent\(fetch\.refresh, "MemoryChanged"\);\s*return fetch;\s*\}/,
  );
  assert.equal(source.includes('"McpStatusChanged"'), false);
}

function verifyMvpCloseoutContracts() {
  const inspector = fs.readFileSync(
    path.join(__dirname, "..", "src", "components", "Inspector.tsx"),
    "utf8",
  );
  const listPanel = fs.readFileSync(
    path.join(__dirname, "..", "src", "components", "ListPanel.tsx"),
    "utf8",
  );
  const workspaceState = fs.readFileSync(
    path.join(__dirname, "..", "src", "lib", "workspace-state.ts"),
    "utf8",
  );
  const hooks = fs.readFileSync(
    path.join(__dirname, "..", "src", "lib", "hooks.ts"),
    "utf8",
  );

  assert.match(workspaceState, /request<WorkspaceStatusResult>\("workspace\.open", \{ path \}\)/);
  assert.match(listPanel, /await openWorkspace\(path\)/);
  assert.match(workspaceState, /request<ContextBootstrapReadResult>\("context\.readBootstrap", \{ name \}\)/);
  assert.match(hooks, /readContextBootstrap\(name\)/);
  assert.match(inspector, /function ActivityPanel\(/);
  assert.match(inspector, /subscribeRuntimeEvents/);
  for (const eventType of [
    "RunStarted",
    "RunCompleted",
    "RunCancelled",
    "ToolCallStarted",
    "ToolProgress",
    "ToolResult",
    "ApprovalRequested",
    "ApprovalResolved",
    "Error",
  ]) {
    assert.match(inspector, new RegExp(`"${eventType}"`));
  }
  assert.equal(
    inspector.includes("Activity stream will show tool calls and events from the running session."),
    false,
  );
  assert.equal(hooks.includes('"McpStatusChanged"'), false);
}

verifySessionContracts()
  .then(() => verifyWorkspaceContracts())
  .then(() => verifyOpsContracts())
  .then(() => verifyToolsPanelRefreshContract())
  .then(() => verifyMemoryHookRefreshContract())
  .then(() => verifyMvpCloseoutContracts())
  .then(() => {
    console.log("desktop frontend contracts ok");
  })
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
