"""
MCP Server Tools for Computer Use

Provides MCP tools (bash, str_replace, create_file, view, sub_agent) via Streamable HTTP.
Works with local Docker socket for container management.

ARCHITECTURE:
- File-server runs alongside Docker daemon on the same host
- Docker containers are created/managed via local Docker socket
- Each chat gets its own isolated container: owui-chat-{chat_id}

GITLAB TOKEN FETCHING:
Priority order for GitLab token:
1. X-Gitlab-Token header (direct from client)
2. MCP Tokens Wrapper (fetches token by user email)
3. No token (continue without GitLab auth)

HTTP Headers for parameters:
Direct headers (priority) or OpenWebUI/LiteLLM headers (alternative):

| Parameter         | Direct Header          | OpenWebUI Header               | Required |
|-------------------|------------------------|--------------------------------|----------|
| Chat ID           | X-Chat-Id              | X-OpenWebUI-Chat-Id            | Yes      |
| User Email        | X-User-Email           | X-OpenWebUI-User-Email         | No       |
| User Name         | X-User-Name            | X-OpenWebUI-User-Name          | No       |
| GitLab Token      | X-Gitlab-Token         | X-OpenWebUI-Gitlab-Token       | No       |
| GitLab Host       | X-Gitlab-Host          | X-OpenWebUI-Gitlab-Host        | No       |
| Anthropic API Key | X-Anthropic-Api-Key    | X-OpenWebUI-Anthropic-Api-Key  | No       |
| Anthropic Base URL| X-Anthropic-Base-Url   | X-OpenWebUI-Anthropic-Base-Url | No       |
| MCP Tokens URL    | X-Mcp-Tokens-Url       | X-OpenWebUI-Mcp-Tokens-Url     | No       |
| MCP Tokens API Key| X-Mcp-Tokens-Api-Key   | X-OpenWebUI-Mcp-Tokens-Api-Key | No       |

Environment Variables:
- MCP_TOKENS_URL: URL of MCP Tokens Wrapper service (optional, for centralized token management)
- MCP_TOKENS_API_KEY: Internal API key for MCP Tokens Wrapper
- SUB_AGENT_DEFAULT_MODEL: Default model for sub_agent (sonnet/opus, default: sonnet)
- SUB_AGENT_MAX_TURNS: Default max turns for sub_agent (default: 30)
- SUB_AGENT_TIMEOUT: Timeout for sub_agent execution in seconds (default: 600)

LiteLLM Integration:
  mcp_servers:
    docker_ai:
      url: "http://localhost:8081/mcp"
      transport: "http"
      auth_type: "bearer_token"
      auth_value: "<MCP_API_KEY>"
      extra_headers:
        # OpenWebUI headers (alternative)
        - "x-openwebui-chat-id"
        - "x-openwebui-user-email"
        - "x-openwebui-user-name"
        - "x-openwebui-gitlab-token"
        - "x-openwebui-gitlab-host"
        - "x-openwebui-anthropic-api-key"
        - "x-openwebui-anthropic-base-url"
        # Direct headers
        - "x-chat-id"
        - "x-user-email"
        - "x-user-name"
        - "x-gitlab-token"
        - "x-gitlab-host"
        - "x-anthropic-api-key"
        - "x-anthropic-base-url"
"""

import os
import re
import json
import shlex
import time
import asyncio
from pathlib import Path
from typing import Optional, List, Annotated
from contextvars import ContextVar

import aiohttp
import docker
from docker.utils.socket import frames_iter, demux_adaptor, consume_socket_output

from mcp.server.fastmcp import FastMCP, Context
from pydantic import Field


# Context variables for request-scoped data (set from HTTP headers)
current_chat_id: ContextVar[str] = ContextVar("current_chat_id", default="default")
current_user_email: ContextVar[Optional[str]] = ContextVar("current_user_email", default=None)
current_user_name: ContextVar[Optional[str]] = ContextVar("current_user_name", default=None)
current_gitlab_token: ContextVar[Optional[str]] = ContextVar("current_gitlab_token", default=None)
current_gitlab_host: ContextVar[str] = ContextVar("current_gitlab_host", default="gitlab.com")
current_anthropic_api_key: ContextVar[Optional[str]] = ContextVar("current_anthropic_api_key", default=None)
current_anthropic_base_url: ContextVar[str] = ContextVar("current_anthropic_base_url", default="https://api.anthropic.com")
current_mcp_tokens_url: ContextVar[str] = ContextVar("current_mcp_tokens_url", default="")
current_mcp_tokens_api_key: ContextVar[str] = ContextVar("current_mcp_tokens_api_key", default="")


# Chat ID validation error message
CHAT_ID_REQUIRED_ERROR = """Error: X-Chat-Id header is required.

Please provide the X-Chat-Id header in your request. This header is used to isolate
your container environment from other users.

If using LiteLLM, ensure extra_headers includes "x-chat-id" or "x-openwebui-chat-id".
If using direct MCP access, add: -H "X-Chat-Id: your-unique-chat-id"
"""


def _validate_chat_id() -> tuple[str, str | None]:
    """
    Validate that chat_id is provided (not default).

    Returns:
        tuple: (chat_id, error_message) - error_message is None if valid
    """
    chat_id = current_chat_id.get()
    if chat_id == "default":
        return chat_id, CHAT_ID_REQUIRED_ERROR
    return chat_id, None


# Configuration from environment
DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "unix:///var/run/docker.sock")
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "computer-use:latest")
CONTAINER_MEM_LIMIT = os.getenv("CONTAINER_MEM_LIMIT", "2g")
CONTAINER_CPU_LIMIT = float(os.getenv("CONTAINER_CPU_LIMIT", "1.0"))
COMMAND_TIMEOUT = int(os.getenv("COMMAND_TIMEOUT", "120"))
ENABLE_NETWORK = os.getenv("ENABLE_NETWORK", "true").lower() == "true"
USER_DATA_BASE_PATH = os.getenv("USER_DATA_BASE_PATH", "/tmp/computer-use-data")
FILE_SERVER_URL = os.getenv("FILE_SERVER_URL", "http://localhost:8081")
CONTAINER_IDLE_TIMEOUT = int(os.getenv("CONTAINER_IDLE_TIMEOUT", "600"))
DEBUG_LOGGING = os.getenv("DEBUG_LOGGING", "false").lower() == "true"
BASE_DATA_DIR = Path(os.getenv("BASE_DATA_DIR", "/data"))

# MCP Tokens Wrapper for GitLab token fetching
MCP_TOKENS_URL = os.getenv("MCP_TOKENS_URL", "")
MCP_TOKENS_API_KEY = os.getenv("MCP_TOKENS_API_KEY", "")

# Sub-agent configuration
SUB_AGENT_DEFAULT_MODEL = os.getenv("SUB_AGENT_DEFAULT_MODEL", "sonnet")
SUB_AGENT_MAX_TURNS = int(os.getenv("SUB_AGENT_MAX_TURNS", "30"))
SUB_AGENT_TIMEOUT = int(os.getenv("SUB_AGENT_TIMEOUT", "600"))


# Global Docker client (lazy init)
_docker_client: Optional[docker.DockerClient] = None


async def _fetch_gitlab_token(email: str, mcp_tokens_url: str, mcp_tokens_api_key: str) -> Optional[str]:
    """
    Fetch decrypted GitLab token from MCP Tokens Wrapper service.

    Args:
        email: User email address
        mcp_tokens_url: URL of MCP Tokens Wrapper service
        mcp_tokens_api_key: Internal API key for authentication

    Returns:
        GitLab token string or None if not found/error
    """
    if not mcp_tokens_api_key:
        print("[GITLAB] MCP_TOKENS_API_KEY not configured, skipping token fetch")
        return None

    if not email:
        print("[GITLAB] No email provided, skipping token fetch")
        return None

    url = f"{mcp_tokens_url}/api/internal/tokens/{email}/gitlab"
    headers = {"X-Internal-Api-Key": mcp_tokens_api_key}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    data = await response.json()
                    token = data.get("token")
                    if token:
                        print(f"[GITLAB] Token fetched for {email}")
                        return token
                elif response.status == 404:
                    print(f"[GITLAB] No token found for {email}")
                else:
                    print(f"[GITLAB] Error fetching token: HTTP {response.status}")
    except asyncio.TimeoutError:
        print(f"[GITLAB] Timeout fetching token for {email}")
    except Exception as e:
        print(f"[GITLAB] Error fetching token: {e}")

    return None


async def _ensure_gitlab_token():
    """
    Ensure GitLab token is available, fetching from MCP Tokens Wrapper if needed.

    Priority:
    1. Token from header (current_gitlab_token already set)
    2. Fetch from MCP Tokens Wrapper by user email
    3. No token (continue without GitLab auth)
    """
    # If token already set from header, use it
    if current_gitlab_token.get():
        return

    # Try to fetch from MCP Tokens Wrapper
    user_email = current_user_email.get()
    mcp_tokens_url = current_mcp_tokens_url.get() or MCP_TOKENS_URL
    mcp_tokens_api_key = current_mcp_tokens_api_key.get() or MCP_TOKENS_API_KEY

    if user_email and mcp_tokens_url and mcp_tokens_api_key:
        token = await _fetch_gitlab_token(user_email, mcp_tokens_url, mcp_tokens_api_key)
        if token:
            current_gitlab_token.set(token)


def get_docker_client() -> docker.DockerClient:
    """Get or create Docker client connected to local socket."""
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.DockerClient(base_url=DOCKER_SOCKET)
        _docker_client.ping()
        print(f"[MCP] Connected to Docker at {DOCKER_SOCKET}")
    return _docker_client


def _build_container_env(extra_env: Optional[dict] = None) -> dict:
    """Build environment variables dict for container."""
    env = {
        "NPM_CONFIG_PREFIX": "/home/assistant/.npm-global",
    }
    if extra_env:
        env.update(extra_env)
    return env


def _get_or_create_container(chat_id: str) -> docker.models.containers.Container:
    """Get existing container or create new one for this chat."""
    client = get_docker_client()

    # Sanitize chat_id for Docker container naming
    sanitized_id = re.sub(r'[^a-zA-Z0-9_.-]', '-', chat_id)
    container_name = f"owui-chat-{sanitized_id}"

    try:
        container = client.containers.get(container_name)
        container.reload()

        if container.status == "exited":
            container.start()
            print(f"[MCP] Started existing container: {container_name}")
        elif container.status == "running":
            if DEBUG_LOGGING:
                print(f"[MCP] Reusing running container: {container_name}")
        else:
            container.start()
            print(f"[MCP] Started container in state '{container.status}': {container_name}")

        return container

    except docker.errors.NotFound:
        print(f"[MCP] Creating new container: {container_name}")
        return _create_container(chat_id, container_name)


def _create_container(chat_id: str, container_name: str) -> docker.models.containers.Container:
    """Create a new persistent container for this chat."""
    client = get_docker_client()

    # Build extra env from context variables
    extra_env = {
        "GITLAB_HOST": current_gitlab_host.get(),
    }

    gitlab_token = current_gitlab_token.get()
    if gitlab_token:
        extra_env["GITLAB_TOKEN"] = gitlab_token
        print(f"[MCP] Injecting GITLAB_TOKEN into container environment")

    anthropic_key = current_anthropic_api_key.get()
    if anthropic_key:
        extra_env["ANTHROPIC_API_KEY"] = anthropic_key
        extra_env["ANTHROPIC_BASE_URL"] = current_anthropic_base_url.get()

    user_name = current_user_name.get()
    user_email = current_user_email.get()
    if user_name:
        extra_env["GIT_AUTHOR_NAME"] = user_name
        extra_env["GIT_COMMITTER_NAME"] = user_name
    if user_email:
        extra_env["GIT_AUTHOR_EMAIL"] = user_email
        extra_env["GIT_COMMITTER_EMAIL"] = user_email

    # Workspace volume for this chat
    workspace_volume = f"chat-{chat_id}-workspace"

    # Host paths for user data
    chat_data_path = os.path.join(USER_DATA_BASE_PATH, chat_id)
    uploads_path = os.path.join(chat_data_path, "uploads")
    outputs_path = os.path.join(chat_data_path, "outputs")

    # Create directories on Docker host with correct permissions
    try:
        print(f"[MCP] Creating directories: {uploads_path}, {outputs_path}")
        client.containers.run(
            image=DOCKER_IMAGE,
            command=f"bash -c 'mkdir -p {shlex.quote(uploads_path)} {shlex.quote(outputs_path)} && chmod -R 777 {shlex.quote(chat_data_path)}'",
            volumes={"/tmp": {"bind": "/tmp", "mode": "rw"}},
            remove=True,
            detach=False,
            user="root"
        )
    except Exception as e:
        print(f"[MCP] Warning: Failed to create directories: {e}")

    # Check if using custom image (has entrypoint) or standard image
    use_entrypoint = "computer-use" in DOCKER_IMAGE

    if use_entrypoint:
        # Production: use entrypoint script
        command = ["bash", "-c", "/home/assistant/.entrypoint.sh bash -c 'trap \"exit 0\" SIGTERM SIGINT; tail -f /dev/null & wait $!'"]
        working_dir = "/home/assistant"
        user = "assistant:assistant"
    else:
        # Development/test: simple bash loop
        command = ["bash", "-c", "trap 'exit 0' SIGTERM SIGINT; tail -f /dev/null & wait $!"]
        working_dir = "/root"
        user = None  # Use image default

    config = {
        "image": DOCKER_IMAGE,
        "name": container_name,
        "hostname": f"chat-{chat_id[:8]}",
        "command": command,
        "detach": True,
        "stdin_open": True,
        "tty": True,
        "mem_limit": CONTAINER_MEM_LIMIT,
        "nano_cpus": int(CONTAINER_CPU_LIMIT * 1_000_000_000),
        "working_dir": working_dir,
        "environment": _build_container_env(extra_env),
        "volumes": {
            workspace_volume: {"bind": working_dir, "mode": "rw"},
            uploads_path: {"bind": "/mnt/user-data/uploads", "mode": "ro"},
            outputs_path: {"bind": "/mnt/user-data/outputs", "mode": "rw"},
        },
        "labels": {
            "managed-by": "mcp-file-server",
            "chat-id": chat_id,
            "tool": "computer-use-mcp"
        },
        "security_opt": ["no-new-privileges:true"],
    }

    if user:
        config["user"] = user

    if not ENABLE_NETWORK:
        config["network_disabled"] = True

    container = client.containers.create(**config)
    container.start()

    print(f"[MCP] Created and started new container: {container_name}")
    return container


def _get_container_user_and_workdir() -> tuple:
    """Get user and workdir based on Docker image type."""
    use_entrypoint = "computer-use" in DOCKER_IMAGE
    if use_entrypoint:
        return "assistant", "/home/assistant"
    else:
        return None, "/root"  # None = use container default


def _reset_shutdown_timer(container):
    """Reset container auto-shutdown timer."""
    user, _ = _get_container_user_and_workdir()
    exec_kwargs = {} if user is None else {"user": user}

    # Kill previous shutdown timer
    container.exec_run(
        "bash -c 'PID=$(cat /tmp/.shutdown-timer-pid 2>/dev/null); [ -n \"$PID\" ] && pkill -P $PID 2>/dev/null; kill $PID 2>/dev/null; true'",
        detach=False,
        **exec_kwargs
    )
    # Start new shutdown timer
    timer_cmd = f"bash -c 'echo $$ > /tmp/.shutdown-timer-pid && sleep {CONTAINER_IDLE_TIMEOUT} && kill 1'"
    container.exec_run(timer_cmd, detach=True, **exec_kwargs)


def _execute_bash(container, command: str, timeout: int = None) -> dict:
    """Execute bash command in container with timeout."""
    _reset_shutdown_timer(container)
    user, workdir = _get_container_user_and_workdir()

    try:
        cmd_timeout = timeout if timeout is not None else COMMAND_TIMEOUT
        timed_command = f"timeout {cmd_timeout} bash -c {shlex.quote(command)}"

        exec_result = container.exec_run(
            cmd=["bash", "-c", timed_command],
            stdout=True,
            stderr=True,
            demux=True,
            workdir=workdir
        )

        stdout_data, stderr_data = exec_result.output if exec_result.output else (b"", b"")
        stdout = stdout_data.decode("utf-8", errors="replace") if stdout_data else ""
        stderr = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

        output = ""
        if stdout:
            output += stdout
        if stderr:
            if output:
                output += "\n"
            output += stderr

        if exec_result.exit_code == 124:
            output += f"\n[Command timed out after {cmd_timeout} seconds]"

        return {
            "exit_code": exec_result.exit_code,
            "output": output,
            "success": exec_result.exit_code == 0
        }

    except Exception as e:
        return {
            "exit_code": -1,
            "output": f"Execution error: {str(e)}",
            "success": False
        }


def _execute_python_with_stdin(container, script: str, data: str) -> dict:
    """Execute Python script in container with data passed through stdin."""
    import socket as sock_module

    _reset_shutdown_timer(container)
    user, workdir = _get_container_user_and_workdir()

    try:
        exec_create_kwargs = {
            "stdin": True,
            "stdout": True,
            "stderr": True,
            "workdir": workdir,
        }
        if user:
            exec_create_kwargs["user"] = user

        exec_id = container.client.api.exec_create(
            container.id,
            ["timeout", str(COMMAND_TIMEOUT), "python3", "-c", script],
            **exec_create_kwargs
        )['Id']

        sock = container.client.api.exec_start(exec_id, socket=True)

        data_bytes = data.encode('utf-8')

        if hasattr(sock, '_sock'):
            sock._sock.sendall(data_bytes)
            sock._sock.shutdown(sock_module.SHUT_WR)
        else:
            sock.sendall(data_bytes)
            if hasattr(sock, 'shutdown_write'):
                sock.shutdown_write()

        gen = frames_iter(sock, tty=False)
        gen = (demux_adaptor(*frame) for frame in gen)
        stdout_data, stderr_data = consume_socket_output(gen, demux=True)

        sock.close()

        exec_info = container.client.api.exec_inspect(exec_id)
        exit_code = exec_info['ExitCode']

        stdout = stdout_data.decode("utf-8", errors="replace") if stdout_data else ""
        stderr = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

        output = ""
        if stdout:
            output += stdout
        if stderr:
            if output:
                output += "\n"
            output += stderr

        if exit_code == 124:
            output += f"\n[Command timed out after {COMMAND_TIMEOUT} seconds]"

        return {
            "exit_code": exit_code,
            "output": output,
            "success": exit_code == 0
        }

    except Exception as e:
        return {
            "exit_code": -1,
            "output": f"Execution error: {str(e)}",
            "success": False
        }


# ============================================================================
# MCP Server Definition
# ============================================================================

mcp = FastMCP(
    name="computer-use-mcp",
    instructions="Computer Use tools via MCP - bash, file operations in Docker containers"
)


# Custom type for view_range
ViewRange = Annotated[
    Optional[List[int]],
    Field(
        default=None,
        min_length=2,
        max_length=2,
        description="Optional line range [start_line, end_line]. Use [start, -1] to view from start to end."
    )
]


@mcp.tool()
async def bash_tool(command: str, description: str) -> str:
    """
    Run a bash command in the container.

    Args:
        command: Bash command to run in container
        description: Why I'm running this command

    Returns:
        Command output (stdout/stderr)
    """
    chat_id, error = _validate_chat_id()
    if error:
        return error

    if DEBUG_LOGGING:
        print(f"[MCP] bash_tool called for chat: {chat_id}")

    try:
        await _ensure_gitlab_token()
        container = await asyncio.to_thread(_get_or_create_container, chat_id)
        result = await asyncio.to_thread(_execute_bash, container, command)
        return result["output"] if result["output"] else f"[Exit code: {result['exit_code']}]"
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
async def str_replace(
    description: str,
    old_str: str,
    path: str,
    new_str: str = ""
) -> str:
    """
    Replace a unique string in a file with another string.
    The string to replace must appear exactly once in the file.

    Args:
        description: Why I'm making this edit
        old_str: String to replace (must be unique in file)
        path: Path to the file to edit
        new_str: String to replace with (empty to delete)

    Returns:
        Success message or error
    """
    chat_id, error = _validate_chat_id()
    if error:
        return error

    if old_str == new_str:
        return "Error: old_str and new_str are identical. No changes would be made."

    try:
        await _ensure_gitlab_token()
        container = await asyncio.to_thread(_get_or_create_container, chat_id)

        script = """
import sys
import json

try:
    data = json.loads(sys.stdin.read())
    path = data['path']
    old_str = data['old_str']
    new_str = data['new_str']

    with open(path, 'r') as f:
        content = f.read()

    if old_str not in content:
        print(f"Error: old_str not found in {path}")
        sys.exit(1)

    new_content = content.replace(old_str, new_str, 1)

    with open(path, 'w') as f:
        f.write(new_content)

    print(f"Successfully replaced text in {path}")
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)
"""
        payload = json.dumps({"path": path, "old_str": old_str, "new_str": new_str})
        result = await asyncio.to_thread(_execute_python_with_stdin, container, script, payload)
        return result["output"]

    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
async def create_file(
    description: str,
    file_text: str,
    path: str
) -> str:
    """
    Create a new file with content in the container.

    Args:
        description: Why I'm creating this file. ALWAYS PROVIDE THIS PARAMETER FIRST.
        file_text: Content to write to the file. ALWAYS PROVIDE THIS PARAMETER SECOND.
        path: Path to the file to create. ALWAYS PROVIDE THIS PARAMETER LAST.

    Returns:
        Success message or error
    """
    chat_id, error = _validate_chat_id()
    if error:
        return error

    try:
        await _ensure_gitlab_token()
        container = await asyncio.to_thread(_get_or_create_container, chat_id)

        script = """
import sys
import json
import os

try:
    data = json.loads(sys.stdin.read())
    path = data['path']
    file_text = data['file_text']

    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, 'w') as f:
        f.write(file_text)

    print(f"Successfully created {path}")
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)
"""
        payload = json.dumps({"path": path, "file_text": file_text})
        result = await asyncio.to_thread(_execute_python_with_stdin, container, script, payload)
        return result["output"] if result["success"] else f"Error: {result['output']}"

    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
async def view(
    description: str,
    path: str,
    view_range: Optional[List[int]] = None
) -> str:
    """
    View text files or directory listings.
    Binary files are detected and rejected with instructions to read SKILL documentation.

    Supported path types:
    - Directories: Lists files and directories with details
    - Text files: Displays numbered lines. You can optionally specify a view_range.
    - Binary files (.xlsx, .docx, .pptx, .pdf, etc.): Returns error with SKILL.md instructions

    Args:
        description: Why I need to view this
        path: Absolute path to file or directory, e.g. `/repo/file.py` or `/repo`
        view_range: Optional line range [start_line, end_line]. Use [start, -1] to view from start to end.

    Returns:
        File contents, directory listing, or error message
    """
    chat_id, error = _validate_chat_id()
    if error:
        return error

    try:
        await _ensure_gitlab_token()
        container = await asyncio.to_thread(_get_or_create_container, chat_id)

        quoted_path = shlex.quote(path)

        # Binary file hints
        binary_file_hints = {
            '.xlsx': 'Excel spreadsheet. Read SKILL first:\n  view /mnt/skills/public/xlsx/SKILL.md',
            '.xls': 'Excel spreadsheet (old). Read SKILL first:\n  view /mnt/skills/public/xlsx/SKILL.md',
            '.docx': 'Word document. Read SKILL first:\n  view /mnt/skills/public/docx/SKILL.md',
            '.pptx': 'PowerPoint. Read SKILL first:\n  view /mnt/skills/public/pptx/SKILL.md',
            '.pdf': 'PDF document. Read SKILL first:\n  view /mnt/skills/public/pdf/SKILL.md',
            '.zip': 'ZIP archive. Use: unzip -l {path}',
            '.tar': 'TAR archive. Use: tar -tvf {path}',
            '.gz': 'Gzip file. Use: gunzip -c {path} | head -n 100',
            '.jpg': 'JPEG image. This is a binary file.',
            '.jpeg': 'JPEG image. This is a binary file.',
            '.png': 'PNG image. This is a binary file.',
            '.gif': 'GIF image. This is a binary file.',
        }

        file_ext = None
        for ext in binary_file_hints.keys():
            if path.lower().endswith(ext):
                file_ext = ext
                break

        if file_ext:
            hint = binary_file_hints[file_ext].format(path=path)
            command = f"""
if [ -f {quoted_path} ]; then
    echo "Error: Cannot view binary file with 'cat'. This is a {file_ext} file."
    echo ""
    echo "{hint}"
    exit 1
elif [ -d {quoted_path} ]; then
    ls -lah {quoted_path}
else
    echo "Error: path not found"
    exit 1
fi
"""
        else:
            if view_range:
                start, end = view_range
                if end == -1:
                    cat_command = f"sed -n '{start},$p' {quoted_path} | cat -n"
                else:
                    cat_command = f"sed -n '{start},{end}p' {quoted_path} | cat -n"
            else:
                cat_command = f"cat -n {quoted_path}"

            command = f"""
if [ -f {quoted_path} ]; then
    {cat_command}
elif [ -d {quoted_path} ]; then
    ls -lah {quoted_path}
else
    echo "Error: path not found"
    exit 1
fi
"""

        result = await asyncio.to_thread(_execute_bash, container, command)
        output = result["output"] if result["output"] else "Error: No output"

        # Truncate if needed
        if not view_range and len(output) > 16000:
            truncation_msg = f"\n\n... [File truncated - middle omitted. Total: {len(output)} chars. Use view_range.] ...\n\n"
            output = output[:8000] + truncation_msg + output[-8000:]

        return output

    except Exception as e:
        return f"Error: {str(e)}"


async def _format_sub_agent_result(
    output: str,
    model: str,
    max_turns: int,
    duration: float
) -> str:
    """Parse Claude JSON output and format result."""
    response_text = ""
    cost = 0.0
    turns = 0
    is_error = False

    try:
        # Find the result JSON line in output
        for line in output.strip().split('\n'):
            line = line.strip()
            if '"type"' in line and '"result"' in line:
                try:
                    parsed = json.loads(line)
                    if parsed.get("type") == "result":
                        response_text = parsed.get("result", "")
                        cost = parsed.get("total_cost_usd", 0.0)
                        turns = parsed.get("num_turns", 0)
                        is_error = parsed.get("is_error", False)
                        break
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"[SUB-AGENT] Failed to parse JSON output: {e}")

    if not response_text:
        response_text = output

    status = "error" if is_error else "success"

    return f"""**Sub-Agent Completed** ({status})
**Model:** {model} | **Turns:** {turns}/{max_turns} | **Cost:** ${cost:.4f} | **Duration:** {duration:.1f}s

{response_text}"""


@mcp.tool()
async def sub_agent(
    task: str,
    description: str,
    model: str = "",
    max_turns: int = 0,
    working_directory: str = "/home/assistant"
) -> str:
    """
    Delegate complex, multi-step tasks to an autonomous sub-agent.

    Use this tool when a task requires:
    - Creating complex presentations or documents
    - Multiple coordinated file operations (multi-file refactoring)
    - Iterative work cycles (run tests, fix, repeat)
    - Research and information gathering
    - Complex analysis with automatic fixes

    Do NOT use for simple tasks you can do directly in 1-2 tool calls.

    Args:
        task: Detailed description of the task for the sub-agent to accomplish
        description: Why you are delegating this task to a sub-agent
        model: Model to use: 'sonnet' (default, fast) or 'opus' (powerful, better for complex tasks)
        max_turns: Maximum number of agentic turns (default from env, typically 30)
        working_directory: Working directory for the agent (default: /home/assistant)

    Returns:
        Sub-agent's response with task results, cost, and turn count
    """
    chat_id, error = _validate_chat_id()
    if error:
        return error

    user_email = current_user_email.get()

    # Use defaults from env if not specified
    if not model:
        model = SUB_AGENT_DEFAULT_MODEL
    if max_turns <= 0:
        max_turns = SUB_AGENT_MAX_TURNS
    if model not in ["sonnet", "opus"]:
        model = "sonnet"

    if DEBUG_LOGGING:
        print(f"[SUB-AGENT] Starting sub-agent ({model}) for chat: {chat_id}")

    try:
        await _ensure_gitlab_token()
        container = await asyncio.to_thread(_get_or_create_container, chat_id)

        # Build the sub-agent system prompt
        file_base_url = f"{FILE_SERVER_URL}/files/{chat_id}"
        system_prompt = f"""<environment>
You are working in a Linux container (Ubuntu 24) as an autonomous sub-agent.
FILE LOCATIONS:
- User uploads: /mnt/user-data/uploads (read-only)
- Workspace: /home/assistant
- Outputs: /mnt/user-data/outputs (URL: {file_base_url}/)
</environment>

<available_skills>
IMPORTANT: Read the relevant SKILL.md BEFORE starting any task!

- docx: /mnt/skills/public/docx/SKILL.md - Word documents creation and editing
- pdf: /mnt/skills/public/pdf/SKILL.md - PDF manipulation, forms, text extraction
- pptx: /mnt/skills/public/pptx/SKILL.md - PowerPoint presentations
- xlsx: /mnt/skills/public/xlsx/SKILL.md - Excel spreadsheets with formulas
- gitlab-explorer: /mnt/skills/public/gitlab-explorer/SKILL.md - GitLab operations (clone, MR, issues)
- skill-creator: /mnt/skills/public/skill-creator/SKILL.md - Creating new skills

Use `cat /mnt/skills/public/<skill>/SKILL.md` to read skill instructions.
</available_skills>"""

        # Build the claude command with user tracking header
        escaped_task = shlex.quote(task)
        escaped_prompt = shlex.quote(system_prompt)

        # ANTHROPIC_CUSTOM_HEADERS passes x-openwebui-user-email header for LiteLLM tracking
        headers_env = ""
        if user_email:
            headers_env = f"ANTHROPIC_CUSTOM_HEADERS={shlex.quote(f'x-openwebui-user-email: {user_email}')} "

        claude_command = (
            f"cd {shlex.quote(working_directory)} && "
            f"{headers_env}"
            f"claude -p {escaped_task} "
            f"--model {model} "
            f"--max-turns {max_turns} "
            f"--permission-mode bypassPermissions "
            f"--append-system-prompt {escaped_prompt} "
            f"--output-format json"
        )

        start_time = time.time()

        # Execute with extended timeout
        result = await asyncio.to_thread(
            _execute_bash,
            container,
            claude_command,
            SUB_AGENT_TIMEOUT
        )

        duration = time.time() - start_time
        output = result.get("output", "")

        # Parse JSON result and format response
        return await _format_sub_agent_result(output, model, max_turns, duration)

    except Exception as e:
        return f"Sub-agent error: {str(e)}"


# ============================================================================
# Helper functions for HTTP header integration
# ============================================================================

def set_context_from_headers(headers: dict):
    """Set context variables from HTTP headers.

    Supports both direct headers (x-chat-id) and OpenWebUI headers (x-openwebui-chat-id).
    Direct headers take priority over OpenWebUI headers.
    """
    # Chat ID (required) - check both formats
    if "x-chat-id" in headers:
        current_chat_id.set(headers["x-chat-id"])
    elif "x-openwebui-chat-id" in headers:
        current_chat_id.set(headers["x-openwebui-chat-id"])

    # User email - check both formats
    if "x-user-email" in headers:
        current_user_email.set(headers["x-user-email"])
    elif "x-openwebui-user-email" in headers:
        current_user_email.set(headers["x-openwebui-user-email"])

    # User name - check both formats
    if "x-user-name" in headers:
        current_user_name.set(headers["x-user-name"])
    elif "x-openwebui-user-name" in headers:
        current_user_name.set(headers["x-openwebui-user-name"])

    # GitLab token - check both formats
    if "x-gitlab-token" in headers:
        current_gitlab_token.set(headers["x-gitlab-token"])
    elif "x-openwebui-gitlab-token" in headers:
        current_gitlab_token.set(headers["x-openwebui-gitlab-token"])

    # GitLab host - check both formats
    if "x-gitlab-host" in headers:
        current_gitlab_host.set(headers["x-gitlab-host"])
    elif "x-openwebui-gitlab-host" in headers:
        current_gitlab_host.set(headers["x-openwebui-gitlab-host"])

    # Anthropic API key - check both formats
    if "x-anthropic-api-key" in headers:
        current_anthropic_api_key.set(headers["x-anthropic-api-key"])
    elif "x-openwebui-anthropic-api-key" in headers:
        current_anthropic_api_key.set(headers["x-openwebui-anthropic-api-key"])

    # Anthropic base URL - check both formats
    if "x-anthropic-base-url" in headers:
        current_anthropic_base_url.set(headers["x-anthropic-base-url"])
    elif "x-openwebui-anthropic-base-url" in headers:
        current_anthropic_base_url.set(headers["x-openwebui-anthropic-base-url"])

    # MCP Tokens URL - check both formats
    if "x-mcp-tokens-url" in headers:
        current_mcp_tokens_url.set(headers["x-mcp-tokens-url"])
    elif "x-openwebui-mcp-tokens-url" in headers:
        current_mcp_tokens_url.set(headers["x-openwebui-mcp-tokens-url"])

    # MCP Tokens API key - check both formats
    if "x-mcp-tokens-api-key" in headers:
        current_mcp_tokens_api_key.set(headers["x-mcp-tokens-api-key"])
    elif "x-openwebui-mcp-tokens-api-key" in headers:
        current_mcp_tokens_api_key.set(headers["x-openwebui-mcp-tokens-api-key"])


def get_mcp_app():
    """Get the MCP ASGI app for mounting."""
    return mcp.streamable_http_app()
