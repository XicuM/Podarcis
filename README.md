# 🦎 Podarcis — The Research and LLM Wiki Agent

| | |
| --- | --- |
| <br>⠀⠀⠀⠀⠀⠀⠀⠀⠠⣽⣆⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀<br>⠀⠀⠀⣤⣤⣤⣤⣄⡚⠻⣿⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀<br>⠀⠀⠀⣿⣿⣿⣿⣿⣿ ⣸⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀<br>⠀⢀⡀⠸⢿⣿⣿⣿⣿⣶⣿⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀<br>⠐⠲⣿⣼⠂ ⣿⣿⣿⣿⣿⣆⠀⢀⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀<br> ⠈⠙⠻⣶⣼⣿⢿⣿⣿⣿⣿⡆⠙⢿⣦⣄⣀⠀⠀⠀⠀⠀⠀⠀⠀<br>⠀⠀⠀⠀⠀⠉⠁⢸⣿⣿⣿⣿⣿⠀⣀⣄⠉⠙⠛⠿⢷⣦⣀⠀⠀⠀<br>⠀⠀⠀⠀⢀⠰⣶⣶⣿⣿⣿⣿⣿⣿⣿⣿⡀⣠⠄⠀⠀⠈⠻⣿⡆⠀<br>⠀⠀⠠⠶⢮⣷⣿⡋⠋⠉⢹⣿⣿⠉⠀⠻⣷⣿⣿⡉⠓⠀⠀⢹⣿⠀<br>⠀⠀⠀⠋⠹⠉⠙⠁⠀⠀⠈⣿⣿⡇⠀⠀⠈⠉⠆⠁⠀⠀⠀⢸⣿⠇<br>⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⢿⣿⣄⠀⠀⠀⠀⠀⠀⠀⢀⣾⣿⠁<br>⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠻⣿⣦⣄⡀⡀⢀⣠⣴⣿⣿⠃⠀<br>⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠛⠿⠿⣿⣿⠿⠿⠋⠁⠀⠀<br> | **Podarcis**<br> *The Research and Wiki Builder Agent* <br><br>Installation:<br>```git clone https://github.com/XicuM/Podarcis.git```<br>```cd Podarcis```<br>```./podarcis install```<br> |

## 🚀 The Workflow Pipeline

Evidence progresses through a strict pipeline with a formal **Hierarchy of Evidence**:

```mermaid
graph TD
    Researcher[Researcher Subagent @researcher] -->|Stages raw sources| Sources[sources/ + state.json]
    Sources -->|Ingested by| Synthesizer[Synthesizer Subagent @synthesizer]
    Synthesizer -->|Writes to| Wiki[wiki/ Objective Knowledge]
    Wiki -->|Tailored by| Architect[Protocol Architect @protocol-architect]
    Architect -->|Writes to| Protocols[workspace/protocols/ Personalized Actions]
    Auditor[Auditor Subagent @auditor] -.->|Validates| Wiki
    Auditor -.->|Validates| Protocols
```

1. **Research (`@researcher`)**: Discovers literature via `research-mcp` (Semantic Scholar), scrapes Google Drive, downloads PDFs, extracts text with markitdown, and enqueues items in `sources/state.json`.
2. **Synthesis (`@synthesizer`)**: Reads pending items from the queue or drive, ingests raw sources, and compiles objective, anonymized knowledge into `wiki/`.
3. **Protocol Architect (`@protocol-architect`)**: Reads `workspace/profile.md`, adapts Wiki findings into step-by-step personalized protocols in `workspace/protocols/` (loading `menumaker` for nutritional protocols).
4. **Audit (`@auditor`)**: Runs continuous machine validation — link integrity (`podarcis lint`), OKF v0.2 frontmatter audits, citation verification, and fact-checking.

---

## 📁 Repository Structure

```text
├── .agents/                 # Core agent personas, MCP servers, and skills
│   ├── agents/              # Subagent personas (researcher, synthesizer, protocol-architect, auditor)
│   ├── mcp/                 # MCP servers (wiki, research, finance, menumaker, gdrive, diagnostics, zoom2okf-mcp)
│   └── skills/              # Domain knowledge (menumaker, harness, self-improvement, python-skill)
├── .opencode/               # OpenCode adapter configuration
│   └── agents -> ../.agents/agents  # Relative symlink for OpenCode subagent integration
├── .podarcis/               # Podarcis runtime engine, TUI CLI, jobs runner, and web server
├── sources/                 # Decoupled Repository: Staging area for raw inputs & ingestion queue
├── wiki/                    # Decoupled Repository: Objective knowledge base (anonymized)
├── workspace/               # Decoupled Repository: Personal profile, feedback, protocols
├── tmp/                     # Temporary scratchpad workspace
├── pyproject.toml           # Python packaging and pytest configuration
└── AGENTS.md                # Agent architecture, conventions, and rules of engagement
```

---

## 🛠 Features & Capabilities

* **Subagent Architecture**: Four specialized subagents (`Researcher`, `Synthesizer`, `Protocol Architect`, `Auditor`) auto-invoked by the primary agent based on task context.
* **Model Context Protocol (MCP)**: Native servers (`research-mcp`, `wiki-mcp`, `finance-mcp`, `menumaker`, `gdrive`, `diagnostics`, `zoom2okf-mcp`) enable literature search, knowledge base queries, nutritional math, and video processing.
* **Modular Podarcis Engine**: The `.podarcis/` Python package provides interactive setup, CLI tools (`podarcis status`, `podarcis test`, `podarcis lint`), background jobs engine, and MCP backend configuration adapters.
* **Hermetic Repositories**: `wiki/`, `workspace/`, and `sources/` are decoupled git repositories ensuring clear separation between objective knowledge and user privacy.
* **Team Habitat & Multi-User Support**: Use [**PodarcisNest**](https://github.com/XicuM/PodarcisLab) for multi-user container orchestration, dynamic reverse proxying, shared OKF knowledge mounts, and Slack research bots.

---

## 🦎 Podarcis Ecosystem

* **[Podarcis](https://github.com/XicuM/Podarcis)** (This Repo): The core evidence-based research engine, FastMCP gateway (`podarcis-mcp`), CLI, and multi-agent personas (`researcher`, `synthesizer`, `protocol-architect`, `auditor`).
* **[PodarcisNest](https://github.com/XicuM/PodarcisLab)**: Multi-user containerized research habitat, Starlette reverse proxy, and Slack Socket Mode daemon for research teams.

---

## ⚙️ Quick Start & Setup

### 1. Clone the Repository
```bash
git clone https://github.com/XicuM/Podarcis.git
cd Podarcis
```

### 2. Run Automated Setup
```bash
./podarcis install
```
This automatically configures the virtual environment, installs dependencies, sets up credentials, links the `podarcis` CLI tool, and clones sub-repositories.

### 3. CLI Quick Reference

| Command | Description |
|---|---|
| `./podarcis install` | Bootstrap venv, dependencies, credentials, and `podarcis` CLI |
| `podarcis status` | Display status of MCP servers, skills, agents, jobs, and repos (`--json` supported) |
| `podarcis config interactive` | Launch interactive TUI configuration menu |
| `podarcis test` | Run test suite across all MCP servers and skills |
| `podarcis lint` | Run link integrity check across wiki markdown files |

### 4. Subagent Quick Reference

| Command | What it does |
|---|---|
| `@researcher <query>` | Searches literature, downloads papers, stages in `sources/` |
| `@synthesizer` | Ingests pending sources into `wiki/` (+ lint, update index) |
| `@protocol-architect <topic>` | Builds personalized protocol in `workspace/protocols/` from wiki + profile |
| `@auditor` | Lint checks, citation audits, cross-reference validation, fact-checking |

---

## 🦎 Salvem ses Sargantanes! (*Podarcis pityusensis*)

> ### 🌿 Salvem ses Sargantanes!
> 
> This project takes its name from *Podarcis*, the genus of agile Mediterranean wall lizards. In particular, the **Ibiza wall lizard** (*Podarcis pityusensis*), endemic to Ibiza and Formentera (*ses sargantanes*), is facing critical threats of extinction due to invasive alien snake species.
> 
> Support active conservation, educational, and habitat protection initiatives:
> 
> 👉 **[Protegim ses Sargantanes — Learn & Support Conservation Efforts](https://protegimsessargantanes.org/en/home-english/)**

