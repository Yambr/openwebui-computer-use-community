"""
File Server for Computer Use Outputs + MCP Endpoint

Provides:
1. HTTP API for file upload/download
2. MCP (Model Context Protocol) endpoint for Computer Use tools

See /docs for Swagger UI, /redoc for ReDoc, / for HTML documentation.
"""

import os
import hashlib
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional, Dict, List, Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Request, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from pydantic import BaseModel, Field


# =============================================================================
# MCP Authorization
# =============================================================================

MCP_API_KEY = os.getenv("MCP_API_KEY")  # Required for /mcp endpoints

security = HTTPBearer(auto_error=False)


async def verify_mcp_auth(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify Bearer token for MCP endpoints."""
    if not MCP_API_KEY:
        # If no key configured, allow all requests (development mode)
        return None

    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"}
        )

    if credentials.credentials != MCP_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"}
        )

    return credentials.credentials


# =============================================================================
# Pydantic Models for Swagger Documentation
# =============================================================================

class MCPRequest(BaseModel):
    """MCP JSON-RPC Request"""
    jsonrpc: str = Field(default="2.0", description="JSON-RPC version")
    id: int = Field(..., description="Request ID")
    method: str = Field(..., description="MCP method: initialize, tools/list, tools/call")
    params: Dict[str, Any] = Field(default={}, description="Method parameters")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"}
                    }
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "bash_tool",
                        "arguments": {"command": "echo hello", "description": "test"}
                    }
                }
            ]
        }
    }


class MCPResponse(BaseModel):
    """MCP JSON-RPC Response"""
    jsonrpc: str = "2.0"
    id: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None


class MCPToolInfo(BaseModel):
    """MCP Tool information"""
    name: str
    description: str


class MCPInfo(BaseModel):
    """MCP Server information"""
    name: str
    version: str
    description: str
    tools: List[MCPToolInfo]
    headers: Dict[str, List[str]]


class UploadResponse(BaseModel):
    """File upload response"""
    status: str
    filename: str
    size: int
    md5: str


# =============================================================================
# FastAPI Application
# =============================================================================

SWAGGER_DESCRIPTION = """
## Computer Use File Server + MCP

HTTP API for file upload/download and **MCP (Model Context Protocol)** endpoint for Computer Use tools.

### Architecture
- File-server runs alongside Docker daemon on the same host
- Each chat gets an isolated Docker container: `owui-chat-{chat_id}`
- Containers are managed via local Docker socket (`/var/run/docker.sock`)
- Files are synchronized between host and containers

### MCP Endpoint

`POST /mcp` - JSON-RPC endpoint for calling Computer Use tools

**Methods:**
- `initialize` - initialize MCP session
- `tools/list` - get list of available tools
- `tools/call` - call a tool

### MCP Authorization

MCP endpoints require Bearer token in `Authorization` header:
```
Authorization: Bearer <MCP_API_KEY>
```

### HTTP Headers for MCP

| Header | Required | Description |
|--------|----------|-------------|
| `Authorization` | **Yes** | Bearer token: `Bearer <API_KEY>` |
| `X-Chat-Id` | **Yes** | Unique chat/session ID |
| `X-User-Email` | No | User email for git config |
| `X-User-Name` | No | User name for git config |
| `X-Gitlab-Token` | No | GitLab token for git/glab authentication |
| `X-Gitlab-Host` | No | GitLab host (default: gitlab.com) |
| `X-Anthropic-Api-Key` | No | Anthropic API key for sub_agent |
| `X-Anthropic-Base-Url` | No | Anthropic base URL |

### MCP Tools

- **bash_tool** - execute bash commands in isolated Docker container
- **view** - view files and directories
- **create_file** - create new files
- **str_replace** - edit files (text replacement)

### Example: Initialize

```bash
curl -X POST http://localhost:8081/mcp \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "X-Chat-Id: my-chat-123" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

### Example: Call bash_tool

```bash
curl -X POST http://localhost:8081/mcp \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "X-Chat-Id: my-chat-123" \\
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"bash_tool","arguments":{"command":"echo hello","description":"test"}}}'
```
"""

app = FastAPI(
    title="Computer Use File Server + MCP",
    description=SWAGGER_DESCRIPTION,
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {"name": "MCP", "description": "Model Context Protocol endpoint for Computer Use tools"},
        {"name": "Files", "description": "File upload and download"},
        {"name": "System", "description": "Health check and service information"},
    ]
)

# Base directory where chat data is stored
# Mounted from host: /tmp/computer-use-data/{chat_id}/outputs/
BASE_DATA_DIR = Path("/data")


@app.get("/", response_class=HTMLResponse, tags=["System"], include_in_schema=False)
async def root():
    """Main page with MCP and File API documentation"""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Computer Use File Server + MCP</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 1000px;
            margin: 0 auto;
            padding: 20px;
            background: #fafafa;
            color: #333;
            line-height: 1.6;
        }
        h1 { color: #1a1a1a; border-bottom: 2px solid #4CAF50; padding-bottom: 10px; }
        h2 { color: #2e7d32; margin-top: 30px; }
        h3 { color: #555; }
        .nav { margin: 20px 0; padding: 15px; background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .nav a { margin-right: 20px; color: #1976d2; text-decoration: none; font-weight: 500; }
        .nav a:hover { text-decoration: underline; }
        .endpoint {
            background: #fff;
            padding: 15px 20px;
            margin: 10px 0;
            border-radius: 8px;
            border-left: 4px solid #4CAF50;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .method {
            display: inline-block;
            font-weight: bold;
            color: #fff;
            padding: 3px 10px;
            border-radius: 4px;
            font-size: 12px;
            margin-right: 10px;
        }
        .get { background: #61affe; }
        .post { background: #49cc90; }
        code {
            background: #e8e8e8;
            padding: 2px 8px;
            border-radius: 4px;
            font-family: 'SF Mono', Monaco, monospace;
            font-size: 14px;
        }
        pre {
            background: #263238;
            color: #aed581;
            padding: 15px;
            border-radius: 8px;
            overflow-x: auto;
            font-size: 13px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin: 15px 0;
            background: #fff;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #eee; }
        th { background: #f5f5f5; font-weight: 600; }
        tr:last-child td { border-bottom: none; }
        .required { color: #d32f2f; font-weight: bold; }
        .tool-list { list-style: none; padding: 0; }
        .tool-list li {
            background: #fff;
            padding: 12px 15px;
            margin: 8px 0;
            border-radius: 8px;
            border-left: 4px solid #1976d2;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .tool-list strong { color: #1976d2; }
        .section { margin-bottom: 30px; }
    </style>
</head>
<body>
    <h1>🖥️ Computer Use File Server + MCP</h1>

    <div class="nav">
        <a href="/docs">📘 Swagger UI</a>
        <a href="/redoc">📗 ReDoc</a>
        <a href="/mcp">🔧 MCP Info</a>
        <a href="/health">❤️ Health</a>
    </div>

    <div class="section">
        <h2>🔌 MCP Endpoint</h2>
        <p>Model Context Protocol (MCP) endpoint for executing commands in isolated Docker containers.</p>

        <div class="endpoint">
            <span class="method post">POST</span>
            <code>/mcp</code>
            <p>JSON-RPC endpoint for calling Computer Use tools</p>
        </div>

        <div class="endpoint">
            <span class="method get">GET</span>
            <code>/mcp</code>
            <p>Information about available MCP tools</p>
        </div>
    </div>

    <div class="section">
        <h3>🔐 MCP Authorization</h3>
        <p>MCP endpoints require Bearer token:</p>
        <pre>Authorization: Bearer YOUR_API_KEY</pre>
    </div>

    <div class="section">
        <h3>📋 HTTP Headers for MCP</h3>
        <table>
            <tr><th>Header</th><th>Required</th><th>Description</th></tr>
            <tr><td><code>Authorization</code></td><td class="required">Yes</td><td>Bearer token: <code>Bearer &lt;API_KEY&gt;</code></td></tr>
            <tr><td><code>X-Chat-Id</code></td><td class="required">Yes</td><td>Unique session/chat ID. Determines Docker container.</td></tr>
            <tr><td><code>X-User-Email</code></td><td>No</td><td>User email for git config</td></tr>
            <tr><td><code>X-User-Name</code></td><td>No</td><td>User name for git config</td></tr>
            <tr><td><code>X-Gitlab-Token</code></td><td>No</td><td>GitLab token for git/glab authentication</td></tr>
            <tr><td><code>X-Gitlab-Host</code></td><td>No</td><td>GitLab host (default: gitlab.com)</td></tr>
            <tr><td><code>X-Anthropic-Api-Key</code></td><td>No</td><td>Anthropic API key for sub_agent</td></tr>
            <tr><td><code>X-Anthropic-Base-Url</code></td><td>No</td><td>Anthropic base URL</td></tr>
        </table>
    </div>

    <div class="section">
        <h3>🛠️ MCP Tools</h3>
        <ul class="tool-list">
            <li><strong>bash_tool</strong> — execute bash commands in isolated Docker container</li>
            <li><strong>view</strong> — view files and directories</li>
            <li><strong>create_file</strong> — create new files</li>
            <li><strong>str_replace</strong> — edit files (text replacement)</li>
            <li><strong>sub_agent</strong> — delegate tasks to autonomous agent (Claude)</li>
        </ul>
    </div>

    <div class="section">
        <h3>📝 Direct MCP Access Examples</h3>

        <h4>List Tools</h4>
        <pre>curl -X POST https://localhost:8081/mcp \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer &lt;MCP_API_KEY&gt;" \\
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'</pre>

        <h4>Call Tool</h4>
        <pre>curl -X POST https://localhost:8081/mcp \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer &lt;MCP_API_KEY&gt;" \\
  -H "X-Chat-Id: my-session-123" \\
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "bash_tool",
      "arguments": {
        "command": "echo Hello from container",
        "description": "Test command"
      }
    }
  }'</pre>
    </div>

    <div class="section">
        <h2>📁 File API</h2>

        <div class="endpoint">
            <span class="method get">GET</span>
            <code>/files/{chat_id}/{filename}</code>
            <p>Download file from container outputs directory</p>
        </div>

        <div class="endpoint">
            <span class="method get">GET</span>
            <code>/files/{chat_id}/archive</code>
            <p>Download all files as ZIP archive</p>
        </div>

        <div class="endpoint">
            <span class="method post">POST</span>
            <code>/api/uploads/{chat_id}/{filename}</code>
            <p>Upload file to container uploads directory</p>
        </div>

        <div class="endpoint">
            <span class="method get">GET</span>
            <code>/api/uploads/{chat_id}/manifest</code>
            <p>Get manifest of uploaded files (filename → MD5)</p>
        </div>
    </div>

    <div class="section">
        <h2>🌐 LiteLLM API Access</h2>
        <p>Access MCP tools through LiteLLM API (<code>api.anthropic.com</code>):</p>

        <h3>Available Tools</h3>
        <ul class="tool-list">
            <li><strong>docker_ai::bash_tool</strong> — execute bash commands</li>
            <li><strong>docker_ai::str_replace</strong> — edit files</li>
            <li><strong>docker_ai::create_file</strong> — create files</li>
            <li><strong>docker_ai::view</strong> — view files and directories</li>
            <li><strong>docker_ai::sub_agent</strong> — delegate tasks to autonomous agent</li>
        </ul>

        <h3>Required Headers</h3>
        <table>
            <tr><th>Header</th><th>Description</th></tr>
            <tr><td><code>X-OpenWebUI-Chat-Id</code></td><td>Unique chat ID (container isolation)</td></tr>
            <tr><td><code>X-OpenWebUI-User-Email</code></td><td>User email</td></tr>
        </table>

        <h3>Request Example</h3>
        <pre>curl -X POST https://api.anthropic.com/v1/chat/completions \\
  -H "Authorization: Bearer &lt;API_KEY&gt;" \\
  -H "X-OpenWebUI-Chat-Id: my-chat-123" \\
  -H "X-OpenWebUI-User-Email: user@example.com" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "claude/claude-sonnet-4-5",
    "messages": [{"role": "user", "content": "Run ls -la"}]
  }'</pre>

        <h3>List MCP Tools</h3>
        <pre>curl -X POST https://api.anthropic.com/mcp/docker_ai/list_tools \\
  -H "Authorization: Bearer &lt;API_KEY&gt;" \\
  -H "Content-Type: application/json" \\
  -H "Accept: application/json, text/event-stream" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'</pre>
    </div>

    <div class="section">
        <h2>🏗️ Architecture</h2>
        <pre>
┌─────────────────────────────────────────────────────────────────┐
│                        Docker Host                              │
│                                                                 │
│  ┌──────────────────┐      ┌─────────────────────────────────┐ │
│  │  File Server     │      │  Docker Containers              │ │
│  │  (this service)  │      │                                 │ │
│  │                  │      │  ┌─────────────────────────┐   │ │
│  │  POST /mcp ──────┼──────┼──│ owui-chat-{chat_id}     │   │ │
│  │                  │      │  │  - bash_tool            │   │ │
│  │  GET /files/* ───┼──────┼──│  - view                 │   │ │
│  │                  │      │  │  - create_file          │   │ │
│  │                  │      │  │  - str_replace          │   │ │
│  └──────────────────┘      │  └─────────────────────────┘   │ │
│           │                │                                 │ │
│           │                │  ┌─────────────────────────┐   │ │
│           ▼                │  │ owui-chat-{other_id}    │   │ │
│  /var/run/docker.sock      │  └─────────────────────────┘   │ │
│                            └─────────────────────────────────┘ │
│                                                                 │
│  /data/{chat_id}/                                              │
│    ├── uploads/   (files from user)                            │
│    └── outputs/   (files created by AI)                        │
└─────────────────────────────────────────────────────────────────┘
        </pre>
    </div>

    <footer style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #ddd; color: #666; font-size: 14px;">
        Computer Use File Server + MCP v2.0.0
    </footer>
</body>
</html>"""


@app.get("/api/uploads/{chat_id}/manifest", tags=["Files"])
async def get_uploads_manifest(chat_id: str) -> Dict[str, str]:
    """
    Get manifest of uploaded files with their MD5 checksums.

    Args:
        chat_id: Unique chat identifier

    Returns:
        Dictionary mapping filename to MD5 checksum
        Example: {"file1.txt": "abc123def456", "doc.pdf": "789xyz"}
    """
    uploads_dir = BASE_DATA_DIR / chat_id / "uploads"

    # Return empty dict if directory doesn't exist yet
    if not uploads_dir.exists():
        return {}

    manifest = {}

    # Scan all files in uploads directory
    for file_path in uploads_dir.rglob("*"):
        if file_path.is_file():
            # Calculate MD5 checksum
            md5_hash = hashlib.md5()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    md5_hash.update(chunk)

            # Use relative filename as key
            relative_name = file_path.relative_to(uploads_dir)
            manifest[str(relative_name)] = md5_hash.hexdigest()

    return manifest


@app.post("/api/uploads/{chat_id}/{filename:path}", tags=["Files"], response_model=UploadResponse)
async def upload_file(chat_id: str, filename: str, file: UploadFile = File(...)):
    """
    Upload a file to chat uploads directory.

    Args:
        chat_id: Unique chat identifier
        filename: Target filename (can include subdirectories)
        file: File to upload

    Returns:
        Success message with file info

    Raises:
        400: Invalid filename or security violation
    """
    # Create uploads directory if it doesn't exist
    uploads_dir = BASE_DATA_DIR / chat_id / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    # Construct target path
    file_path = uploads_dir / filename

    # Security: ensure path is within uploads directory
    try:
        file_path = file_path.resolve()
        if not str(file_path).startswith(str(uploads_dir.resolve())):
            raise HTTPException(status_code=403, detail="Access denied: path traversal detected")
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid path")

    # Create parent directories if needed
    file_path.parent.mkdir(parents=True, exist_ok=True)

    # Save file
    try:
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)

        # Calculate MD5 for confirmation
        md5_hash = hashlib.md5(content).hexdigest()

        return {
            "status": "success",
            "filename": filename,
            "size": len(content),
            "md5": md5_hash
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")


@app.get("/files/{chat_id}/archive", tags=["Files"])
async def download_archive(chat_id: str):
    """
    Download entire outputs directory as a zip archive.

    Args:
        chat_id: Unique chat identifier

    Returns:
        StreamingResponse with zip archive

    Raises:
        404: Directory not found or empty
    """
    # Construct outputs directory path
    outputs_dir = BASE_DATA_DIR / chat_id / "outputs"

    # Check if directory exists
    if not outputs_dir.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Outputs directory not found for chat: {chat_id}"
        )

    if not outputs_dir.is_dir():
        raise HTTPException(
            status_code=400,
            detail="Path is not a directory"
        )

    # Get all files in directory
    files = list(outputs_dir.rglob("*"))
    files = [f for f in files if f.is_file()]

    if not files:
        raise HTTPException(
            status_code=404,
            detail="No files found in outputs directory"
        )

    # Create zip archive in memory
    zip_buffer = BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for file_path in files:
            # Add file to zip with relative path
            arcname = file_path.relative_to(outputs_dir)
            zip_file.write(file_path, arcname=str(arcname))

    # Seek to beginning of buffer
    zip_buffer.seek(0)

    # Return as streaming response
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=chat-{chat_id}-outputs.zip"
        }
    )


@app.get("/files/{chat_id}/{filename:path}", tags=["Files"])
async def download_file(chat_id: str, filename: str):
    """
    Download a specific file from chat outputs directory.

    Args:
        chat_id: Unique chat identifier
        filename: File name (can include subdirectories)

    Returns:
        FileResponse with the requested file

    Raises:
        404: File not found
    """
    # Construct full path
    file_path = BASE_DATA_DIR / chat_id / "outputs" / filename

    # Security: ensure path is within allowed directory
    try:
        file_path = file_path.resolve()
        BASE_DATA_DIR.resolve()
        if not str(file_path).startswith(str(BASE_DATA_DIR.resolve())):
            raise HTTPException(status_code=403, detail="Access denied")
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid path")

    # Check if file exists
    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"File not found: {filename}"
        )

    if not file_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Path is not a file: {filename}"
        )

    # Return file
    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="application/octet-stream"
    )


@app.get("/health", tags=["System"])
async def health():
    """Health check for monitoring"""
    return {"status": "healthy"}


# ============================================================================
# MCP Endpoint Integration
# ============================================================================

# MCP requires initialization via lifespan context manager
# We'll use a custom SSE-based approach for better compatibility

_mcp_server = None
_mcp_set_context = None


def _init_mcp():
    """Initialize MCP server (lazy load)."""
    global _mcp_server, _mcp_set_context
    if _mcp_server is None:
        try:
            from mcp_tools import mcp, set_context_from_headers
            _mcp_server = mcp
            _mcp_set_context = set_context_from_headers
            print("[MCP] MCP server initialized")
        except ImportError as e:
            print(f"[MCP] Warning: MCP tools not available: {e}")
    return _mcp_server, _mcp_set_context


@app.post("/mcp", tags=["MCP"], response_model=MCPResponse)
async def mcp_endpoint(request: Request, _auth: str = Depends(verify_mcp_auth)):
    """
    MCP Streamable HTTP endpoint for Computer Use tools.

    Available tools:
    - bash_tool: Run bash commands in isolated Docker container
    - str_replace: Edit files by replacing text
    - create_file: Create new files
    - view: View files and directories

    Required Headers:
    - X-Chat-Id: Unique chat identifier

    Optional Headers:
    - X-User-Email: User email for git config
    - X-User-Name: User name for git config
    - X-Gitlab-Token: GitLab token for authentication
    - X-Gitlab-Host: GitLab host (default: gitlab.com)
    - X-Anthropic-Api-Key: Anthropic API key for sub_agent
    - X-Anthropic-Base-Url: Anthropic base URL

    Example usage with MCP client:
    ```python
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        "http://localhost:8081/mcp",
        headers={"X-Chat-Id": "my-chat-123"}
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("bash_tool", {
                "command": "echo hello",
                "description": "Test command"
            })
    ```
    """
    mcp_server, set_context = _init_mcp()

    if mcp_server is None:
        raise HTTPException(
            status_code=503,
            detail="MCP endpoint not available. Missing dependencies (mcp, docker)."
        )

    # Set context from headers
    headers = {k.lower(): v for k, v in request.headers.items()}
    set_context(headers)

    # Parse JSON-RPC request
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    method = body.get("method")
    params = body.get("params", {})
    request_id = body.get("id")

    # Handle MCP methods
    try:
        if method == "initialize":
            # Return server capabilities
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False}
                },
                "serverInfo": {
                    "name": "computer-use-mcp",
                    "version": "1.0.0"
                }
            }
        elif method == "tools/list":
            # List available tools
            tools = []
            for tool in mcp_server._tool_manager.list_tools():
                tools.append({
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.parameters
                })
            result = {"tools": tools}
        elif method == "tools/call":
            # Call a tool
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})

            # Find and execute tool
            tool_result = await mcp_server._tool_manager.call_tool(tool_name, tool_args)
            result = {
                "content": [{"type": "text", "text": str(tool_result)}],
                "isError": False
            }
        elif method == "notifications/initialized":
            # Client initialized notification - no response needed
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        else:
            raise HTTPException(status_code=400, detail=f"Unknown method: {method}")

        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32000, "message": str(e)}
        }


@app.get("/mcp", tags=["MCP"], response_model=MCPInfo)
async def mcp_info(_auth: str = Depends(verify_mcp_auth)):
    """Get MCP endpoint information."""
    mcp_server, _ = _init_mcp()

    if mcp_server is None:
        raise HTTPException(
            status_code=503,
            detail="MCP endpoint not available"
        )

    tools = []
    for tool in mcp_server._tool_manager.list_tools():
        tools.append(MCPToolInfo(
            name=tool.name,
            description=tool.description.strip() if tool.description else ""
        ))

    return MCPInfo(
        name="computer-use-mcp",
        version="1.0.0",
        description="Computer Use tools via MCP - execute commands in isolated Docker containers",
        tools=tools,
        headers={
            "required": ["X-Chat-Id"],
            "optional": [
                "X-User-Email",
                "X-User-Name",
                "X-Gitlab-Token",
                "X-Gitlab-Host",
                "X-Anthropic-Api-Key",
                "X-Anthropic-Base-Url"
            ]
        }
    )
