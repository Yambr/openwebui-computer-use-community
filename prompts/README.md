# Claude Code Prompts

System prompts extracted from **Claude Code CLI v2.1.9** — Anthropic's official CLI.

These prompts can be used as a reference for creating your own AI agents and tools.

---

## Structure

```
prompts/
├── agents/      # Sub-agents for specialized tasks
├── modes/       # Output modes (communication styles)
├── skills/      # Built-in skills (commands)
├── system/      # System prompts
└── tools/       # Tool descriptions
```

---

## Agents — Sub-agents

Sub-agents are launched via the `Task` tool to perform specialized tasks.

| File | Agent | Model | Description |
|------|-------|-------|-------------|
| [bash.md](agents/bash.md) | `Bash` | inherit | Bash command execution. Git operations, terminal. |
| [explore.md](agents/explore.md) | `Explore` | **haiku** | Fast codebase search. READ-ONLY mode. |
| [plan.md](agents/plan.md) | `Plan` | inherit | Architectural planning. READ-ONLY mode. |
| [general-purpose.md](agents/general-purpose.md) | `general-purpose` | inherit | Multi-purpose agent for complex tasks. |
| [statusline-setup.md](agents/statusline-setup.md) | `statusline-setup` | sonnet | CLI status line configuration. |
| [claude-code-guide.md](agents/claude-code-guide.md) | `claude-code-guide` | inherit | Reference for Claude Code, Agent SDK, API. |

### When to Use

- **Explore** — when you need to quickly find files/code. Uses haiku for speed.
- **Plan** — when you need to plan feature implementation.
- **Bash** — for git operations and terminal commands.
- **general-purpose** — when the task is complex and doesn't fit other agents.

---

## Modes — Output Modes

Change Claude's communication style. Added to the main system prompt.

| File | Mode | Description |
|------|------|-------------|
| [explanatory.md](modes/explanatory.md) | `Explanatory` | Explains implementation choices, provides insights |
| [learning.md](modes/learning.md) | `Learning` | Asks user to write code for practice |

### Feature: Insights

Both modes use the `★ Insight` block for educational explanations:

```
★ Insight ─────────────────────────────────────
[2-3 key points]
─────────────────────────────────────────────────
```

---

## Skills — Skills

Built-in commands called via `/command`.

| File | Command | Description |
|------|---------|-------------|
| [code-review.md](skills/code-review.md) | `/review` | PR review via `gh` CLI |
| [security-review.md](skills/security-review.md) | `/security` | Security audit of branch changes |

### Security Review — Details

The most detailed prompt (164 lines). Includes:
- Vulnerability categories (SQL injection, XSS, RCE, etc.)
- Analysis methodology (3 phases)
- Severity guidelines (HIGH/MEDIUM/LOW)
- Confidence scoring (0.7-1.0)
- **FALSE POSITIVE FILTERING** — 17 rules for excluding false positives

---

## System — System Prompts

| File | Description |
|------|-------------|
| [summary.md](system/summary.md) | Prompt for context compaction (summary/compact) |

### Summary Prompt

Used when context overflows. Claude creates a detailed summary:
1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections
4. Errors and fixes
5. Problem Solving
6. All user messages
7. Pending Tasks
8. Current Work
9. Optional Next Step

---

## Tools — Tool Descriptions

Descriptions of tools that Claude sees in the system prompt.

| File | Tool | Lines | Description |
|------|------|-------|-------------|
| [bash.md](tools/bash.md) | `Bash` | 130 | Command execution + **Git Commit/PR instructions** |
| [read.md](tools/read.md) | `Read` | 16 | Read files (including images, PDF, notebooks) |
| [write.md](tools/write.md) | `Write` | 7 | Write files |
| [glob.md](tools/glob.md) | `Glob` | 5 | File search by pattern |
| [grep.md](tools/grep.md) | `Grep` | 9 | Content search (ripgrep) |

### Bash Tool — Key

The largest tool prompt. Contains:
- Directory verification
- Command execution guidelines
- **Git Safety Protocol** — rules for safe git usage
- **Git Commit workflow** — how to commit properly
- **PR Creation workflow** — how to create Pull Requests

---

## How It Works in Claude Code

Prompts are assembled at runtime:

```
Base System Prompt
  + Tone & Style (if no output style)
  + Task Management (if TodoWrite exists)
  + Tool Descriptions
  + Output Style (modes/)
  + Agent Definitions
  = Final system prompt
```

Each file is a self-contained module.

---

## Usage

### As Reference
```python
# Load agent prompt
with open("prompts/agents/explore.md") as f:
    explore_prompt = f.read()

# Use in your agent
system_prompt = f"""
{explore_prompt}

Additional instructions for my use case...
"""
```

### Update
When Claude Code is updated, you can re-run extraction:
```bash
node extract_prompts.js
```

---

## Source

```
~/.nvm/versions/node/v23.3.0/lib/node_modules/@anthropic-ai/claude-code/cli.js
```

Version: **2.1.9** (January 2026)

---

*Extracted with Claude Opus 4.5*
