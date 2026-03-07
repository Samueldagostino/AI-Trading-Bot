# Quick Start: Claude Code Agent Teams for NQ Trading Bot

## Step 1 — Install Claude Code (if not already)

```bash
npm install -g @anthropic-ai/claude-code
```

Requires Node.js 18+. You can use it with your existing Pro, Max, or Team subscription.


## Step 2 — Place files in your project root

Copy these two files into the ROOT of your nq-trading-bot folder
(the same folder that contains main.py):

  CLAUDE.md          ← The project brain. Agents read this on startup.
  setup_validate.py  ← Run once to verify everything is in place.

Your folder should look like:

  nq-trading-bot/
  ├── CLAUDE.md            ← NEW
  ├── setup_validate.py    ← NEW
  ├── main.py
  ├── config/
  ├── execution/
  ├── features/
  ├── signals/
  ├── risk/
  └── ...


## Step 3 — Validate

```bash
cd /path/to/your/nq-trading-bot
python setup_validate.py
```

This checks that all critical files exist, HC filter constants are in place,
and CLAUDE.md is readable. Fix any [!!] items before proceeding.


## Step 4 — Enable Agent Teams

Agent Teams is experimental. Enable it:

```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
```

Or add it to your Claude Code settings.json (usually ~/.claude/settings.json):

```json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```


## Step 5 — Launch Claude Code

```bash
cd /path/to/your/nq-trading-bot
claude
```

Claude Code will automatically read CLAUDE.md and understand the full
project architecture, the HC filter rules, and the codebase layout.


## Step 6 — Use Agent Teams

Example prompts to the lead agent:

### Single task (no team needed):
  "Tighten the stop cap from 30 to 25 points and run the backtest."

### Multi-agent team task:
  "Spawn a team of 3 agents:
   1. Strategy agent: Add a regime gate that blocks entries during
      trending_down. Update main.py only.
   2. Dashboard agent: Build a TradingView-style chart tab showing
      trade entries/exits overlaid on the price chart.
   3. QA agent: After both are done, run the backtest and validate
      that HC filter metrics haven't degraded below PF 2.0."

### Research/debug team:
  "Spawn 3 agents to investigate why C2 trailing stops are exiting
   too early in ranging regimes. Have them each test a different
   hypothesis and debate their findings."


## Key Things to Know

- Each teammate reads CLAUDE.md automatically (your project context)
- Teammates do NOT share conversation history with the lead
- Include specific context in spawn prompts for best results
- Token cost scales with number of agents — use teams for big tasks
- For small edits, a single Claude Code session is more efficient
- Always validate changes against the baseline metrics in CLAUDE.md
