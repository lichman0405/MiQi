---
name: pievo
description: >
  Autonomous scientific discovery via principle evolution.
  Trigger when the user wants to optimize experimental parameters,
  discover material design principles, or run multi-round automated
  experiments. Requires pievo.enabled=true in config AND scientific
  MCP tools (zeopp, raspa, mofstructure, mofchecker) to be available.
pinned: true
metadata: {"miqi":{"emoji":"🔬","requires":{"mcp":[]}}}
---

# PiEvo — Principle-Evolvable Experiment Selection

## When to use
- User asks to optimize parameters ("find the best MOF for CO2 capture")
- User wants to discover scientific principles from experimental data
- User wants to automate a multi-round experimental campaign
- User mentions "贝叶斯优化", "实验设计", "自动发现", "原则进化"

## Prerequisites
1. `pievo.enabled` must be true: check with `miqi config get tools.pievo.enabled`
2. At least one scientific MCP tool must be connected (zeopp-backend, raspa-mcp, etc.)
3. If dependencies are missing, tell user to run: `pip install -e ".[pievo]"`

## Workflow

### Step 1: Understand the task
Extract from user input:
- Parameter space (what can be varied?)
- Optimization target (what metric to maximize/minimize?)
- Constraints (any hard limits?)
- Available tools (which MCP scientific tools are relevant?)

### Step 2: Launch discovery session
```
pievo_run(
    task="<natural language task description including parameters, target, constraints>",
    tools=["<mcp_tool_name_1>", "<mcp_tool_name_2>"],
    max_rounds=20,
    warm_up_rounds=5
)
```

### Step 3: Monitor progress
```
pievo_status("<session_id>")
```

### Step 4: Report results
When the session completes, summarize for the user:
- Discovered principles and their belief scores
- Best parameters found
- Number of rounds executed
- Convergence trend

## Important Notes
- PiEvo uses 3 LLM calls per round (Principle, Hypothesis, Experiment phases)
- The GP model needs warm_up_rounds of data before IDS activates
- If a tool call fails, the round is recorded with outcome=0.0 and PiEvo continues
- Sessions run asynchronously — use pievo_status to monitor, not polling in a loop
