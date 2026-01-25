# Open WebUI Computer Use

A Claude.ai-like virtual machine for Open WebUI.

## What is this?

This is a Docker container that replicates the Claude.ai "computer use" environment:

- **Same Python packages** as Claude.ai's sandbox (document processing, image manipulation, web automation, ML libraries)
- **Same skills system** with publicly available Claude skills (docx, pptx, xlsx, pdf, etc.)
- **Additional tools**: `glab` (GitLab CLI), `claude` (Claude Code CLI)

Nothing special — just a pre-configured environment that gives Open WebUI the same capabilities Claude.ai has when running code.

## Quick Start

### 1. Pull the Image

```bash
docker pull yambr/openwebui-computer-use:latest
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — add your ANTHROPIC_API_KEY
```

### 3. Run

```bash
docker-compose up -d
```

### 4. Install in Open WebUI

1. **Tools**: Import `openwebui-tools/computer_use_tools.py`
2. **Filter**: Import `openwebui-functions/computer_link_filter.py`
3. Enable the filter globally or per-model

## What's Inside

### Python Packages

Document processing, PDF, images, OCR, web scraping, data science, ML — see `requirements.txt` for full list.

### Pre-installed Skills

| Skill | Description |
|-------|-------------|
| `docx` | Word documents |
| `pptx` | PowerPoint presentations |
| `xlsx` | Excel spreadsheets |
| `pdf` | PDF creation and manipulation |
| `gitlab-explorer` | GitLab repository exploration |

### CLI Tools

- `glab` — GitLab CLI
- `claude` — Claude Code CLI
- `playwright` — Browser automation

## Architecture

```
Open WebUI → File Server (port 8081) → Docker Container
                                        ├── Python 3.12
                                        ├── Node.js 22
                                        ├── Playwright
                                        └── LibreOffice
```

Each chat session gets an isolated container. Files are stored in `/tmp/computer-use-data/{chat_id}/`.

## Configuration

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `ANTHROPIC_BASE_URL` | API endpoint (default: `https://api.anthropic.com`) |
| `MCP_API_KEY` | MCP auth key (auto-generated) |

## Docker Hub

```bash
# Pull
docker pull yambr/openwebui-computer-use:latest

# Or build locally
docker-compose build ai-computer-use
```

## License

MIT License
