"""
title: Computer Use Tools
author: OpenWebUI Implementation
version: 2.3.0

Provides AI Computer Use tools (bash, str_replace, file_create, view, sub_agent) in persistent Docker containers.
Each chat gets its own isolated container that persists between tool calls.

Based on AI Computer Use system prompt - provides Linux environment access.

ARCHITECTURE:
- OpenWebUI connects to Docker daemon (local or remote via SSH)
- Each chat session gets an isolated Docker container
- File server provides file upload/download endpoints

DEPLOYMENT:
1. Connection Setup:
   - Configure DOCKER_SOCKET for local Docker daemon connection
   - Or configure DOCKER_SSH_HOST and DOCKER_SSH_USER for remote connection

2. Install this tool in OpenWebUI:
   - IMPORTANT: Set Tool ID to "ai_computer_use" in OpenWebUI interface
   - (Settings → Tools → Edit this tool → ID field = "ai_computer_use")
   - This is required for the Computer Use Filter to detect and inject system prompt

3. File Server must be accessible:
   - Default URL: http://localhost:8081
   - Used for file uploads and downloads

Container naming: owui-chat-{chat_id}

File Upload Handling:
- Files uploaded in OpenWebUI are stored in file-server at /tmp/computer-use-data/{chat_id}/uploads/
- Containers mount this directory as read-only at /mnt/user-data/uploads/
- Tools automatically discover available files when commands reference the uploads directory
- No automatic sync needed - files are available via bind mount
- File discovery is on-demand: only when bash command or file path references uploads directory

REQUIRED SETUP:
- Tool ID MUST be "ai_computer_use" for system prompt injection to work
- Companion filter "Computer Use Filter" (computer_link_filter.py) must be installed and enabled
- Filter automatically injects Computer Use system prompt when this tool is active
"""

import asyncio
import docker
import shlex
import re
import json
import os
import time
import aiohttp
from typing import Callable, Awaitable, Optional, Tuple, List, Annotated
from pydantic import BaseModel, Field, BeforeValidator
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema
from docker.utils.socket import frames_iter, demux_adaptor, consume_socket_output


# Custom type for view_range - using List instead of Tuple to avoid schema generation issues
# Gemini/VertexAI doesn't accept empty items: {} in array schema
ViewRange = Annotated[
    Optional[List[int]],
    Field(
        default=None,
        min_length=2,
        max_length=2,
        description="Optional line range for text files. Format: [start_line, end_line] where lines are indexed starting at 1. Use [start_line, -1] to view from start_line to the end of the file. When not provided, the entire file is displayed, truncating from the middle if it exceeds 16,000 characters (showing beginning and end)."
    )
]


async def _fetch_gitlab_token(email: str, mcp_tokens_url: str, mcp_tokens_api_key: str) -> Optional[str]:
    """
    Fetch decrypted GitLab token from MCP Tokens Wrapper service.
    Module-level function to avoid exposing to AI via Tools class.

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


async def _get_mcp_servers_for_user(request, user_email: str) -> list:
    """
    Get MCP servers from OpenWebUI config that are available to the user.

    Args:
        request: FastAPI request object with app.state.config
        user_email: User email for access control filtering

    Returns:
        List of MCP server configurations in OpenWebUI format
    """
    if not request or not hasattr(request, 'app'):
        return []

    try:
        tool_server_connections = request.app.state.config.TOOL_SERVER_CONNECTIONS
        mcp_servers = []

        for server in tool_server_connections:
            # Only MCP servers
            if server.get("type") != "mcp":
                continue

            # Only enabled servers
            if not server.get("config", {}).get("enable", True):
                continue

            # TODO: Add access_control check for user permissions
            # For now, include all enabled MCP servers

            mcp_servers.append(server)

        return mcp_servers
    except Exception as e:
        print(f"[MCP] Error getting servers from OpenWebUI: {e}")
        return []


def _convert_owui_mcp_to_claude_format(owui_servers: list, user_email: str) -> dict:
    """
    Convert MCP servers from OpenWebUI format to Claude Code .mcp.json format.

    OpenWebUI format:
    {
        "url": "https://api.example.com/mcp",
        "type": "mcp",
        "auth_type": "bearer",
        "headers": {"X-Custom": "value"},
        "key": "api_key",
        "info": {"id": "server-id", "name": "Server Name"}
    }

    Claude Code .mcp.json format:
    {
        "server-id": {
            "type": "http",
            "url": "https://api.example.com/mcp",
            "headers": {
                "Authorization": "Bearer api_key",
                "x-openwebui-user-email": "user@example.com"
            }
        }
    }

    Args:
        owui_servers: List of OpenWebUI MCP server configurations
        user_email: User email to add to headers

    Returns:
        Dict in Claude Code mcpServers format
    """
    claude_mcp = {}

    for server in owui_servers:
        server_id = server.get("info", {}).get("id", "")
        if not server_id:
            continue

        url = server.get("url", "")
        if not url:
            continue

        # Build headers
        headers = {}

        # Add user email header
        if user_email:
            headers["x-openwebui-user-email"] = user_email

        # Auth headers
        auth_type = server.get("auth_type", "none")
        if auth_type == "bearer" and server.get("key"):
            headers["Authorization"] = f"Bearer {server.get('key')}"

        # Custom headers from config
        custom_headers = server.get("headers", {})
        if isinstance(custom_headers, dict):
            headers.update(custom_headers)

        claude_mcp[server_id] = {
            "type": "http",
            "url": url,
            "headers": headers
        }

    return claude_mcp


class _ContainerManager:
    """
    Internal helper class for Docker container management.
    Not exposed to OpenWebUI API.
    """

    def __init__(self, docker_client, valves, mcp_tokens_url: str = "", mcp_tokens_api_key: str = ""):
        self.client = docker_client
        self.valves = valves
        self.mcp_tokens_url = mcp_tokens_url
        self.mcp_tokens_api_key = mcp_tokens_api_key

    async def get_container(self, chat_id: str, extra_env: Optional[dict] = None):
        """Get/create container with env vars.

        Args:
            chat_id: Chat ID for container naming
            extra_env: Environment variables to pass to container (constructed by caller)
        """
        return await asyncio.to_thread(
            self.get_or_create_container, chat_id, extra_env
        )

    def _build_container_env(self, extra_env: Optional[dict] = None) -> dict:
        """
        Build environment variables dict for container.

        Args:
            extra_env: Optional dict of additional environment variables

        Returns:
            Dict of environment variables to pass to container
        """
        env = {}

        if extra_env:
            env.update(extra_env)
            if "GITLAB_TOKEN" in extra_env:
                print(f"[GITLAB] Injecting GITLAB_TOKEN into container environment")

        return env

    def _ensure_connection(self):
        """
        Lightweight SSH connection check - only ping, fast.
        Reconnects if connection is broken.

        Returns:
            bool: True if connection is active, False if failed to reconnect
        """
        import time
        start = time.time()

        try:
            if self.client:
                self.client.ping()
                elapsed_ms = (time.time() - start) * 1000
                if self.valves.DEBUG_LOGGING:
                    print(f"[SSH-CHECK] ping took {elapsed_ms:.1f}ms")
                return True
        except Exception as e:
            elapsed_ms = (time.time() - start) * 1000
            error_msg = str(e).lower()
            # Check for SSH/connection errors
            if any(kw in error_msg for kw in ["ssh session not active", "connection", "broken pipe"]):
                print(f"[SSH-CHECK] Detected broken connection after {elapsed_ms:.1f}ms: {e}")
                return self._reconnect_docker()
        return False

    def _reconnect_docker(self):
        """Reconnect to Docker daemon (SSH or local)"""
        import time

        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Close old client if exists
                if self.client:
                    try:
                        self.client.close()
                    except:
                        pass

                # Create new client
                if self.valves.DOCKER_SSH_HOST and self.valves.DOCKER_SSH_USER:
                    docker_url = f"ssh://{self.valves.DOCKER_SSH_USER}@{self.valves.DOCKER_SSH_HOST}"
                    print(f"[RECONNECT] Connecting to Docker via SSH: {docker_url} (attempt {attempt + 1}/{max_retries})")
                    self.client = docker.DockerClient(base_url=docker_url)
                else:
                    print(f"[RECONNECT] Connecting to local Docker socket (attempt {attempt + 1}/{max_retries})")
                    self.client = docker.DockerClient(base_url=self.valves.DOCKER_SOCKET)

                # Test connection
                self.client.ping()
                print("[RECONNECT] Successfully reconnected to Docker")
                return True

            except Exception as e:
                print(f"[RECONNECT] Attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff: 1, 2, 4 seconds

        print("[RECONNECT] Failed to reconnect after all retries")
        self.client = None
        return False

    def _execute_with_retry(self, operation, *args, **kwargs):
        """
        Execute Docker operation with automatic retry on SSH errors.

        Args:
            operation: Function to execute (e.g., self.client.containers.get)
            *args, **kwargs: Arguments to pass to the operation

        Returns:
            Result of the operation

        Raises:
            Exception if operation fails after retries
        """
        max_attempts = 2

        for attempt in range(max_attempts):
            try:
                if not self.client:
                    raise Exception("Docker client not initialized")

                return operation(*args, **kwargs)

            except Exception as e:
                error_msg = str(e).lower()
                is_ssh_error = any(keyword in error_msg for keyword in [
                    "ssh session not active",
                    "connection refused",
                    "connection reset",
                    "broken pipe",
                    "connection aborted"
                ])

                if is_ssh_error and attempt < max_attempts - 1:
                    print(f"[RETRY] Detected SSH connection error: {e}")
                    print(f"[RETRY] Attempting to reconnect...")

                    if self._reconnect_docker():
                        print(f"[RETRY] Retrying operation after reconnection...")
                        continue
                    else:
                        raise Exception("Failed to reconnect to Docker after SSH session error")
                else:
                    # Not SSH error or last attempt - raise original exception
                    raise

    def discover_and_list_files(self, chat_id: str) -> list:
        """
        Discover available files in file-server uploads directory.
        This replaces the need for __files__ parameter which is not provided to Tools.

        Args:
            chat_id: Chat identifier

        Returns:
            List of filenames available in file-server uploads directory
        """
        import requests

        try:
            manifest_url = f"{self.valves.FILE_SERVER_URL}/api/uploads/{chat_id}/manifest"
            response = requests.get(manifest_url, timeout=5)
            response.raise_for_status()
            manifest = response.json()
            return list(manifest.keys())
        except Exception:
            return []

    def sync_uploaded_files(self, chat_id: str, files: list) -> dict:
        """
        Sync uploaded files from OpenWebUI to container uploads directory via file-server.

        Args:
            chat_id: Chat identifier
            files: List of file dicts from __files__ parameter
                   Format: [{"path": "/path/in/openwebui", "name": "filename.ext"}, ...]

        Returns:
            Dict with sync results: {"synced": 2, "skipped": 1, "errors": 0, "details": [...]}
        """
        import requests
        import hashlib

        if self.valves.DEBUG_LOGGING:
            print(f"[SYNC] Starting file sync for chat {chat_id}")
            print(f"[SYNC] Files to sync: {len(files) if files else 0}")
            print(f"[SYNC] File-server URL: {self.valves.FILE_SERVER_URL}")

        if not files:
            if self.valves.DEBUG_LOGGING:
                print(f"[SYNC] No files provided, skipping sync")
            return {"synced": 0, "skipped": 0, "errors": 0, "details": []}

        # Get manifest from file-server (current state of uploads)
        try:
            manifest_url = f"{self.valves.FILE_SERVER_URL}/api/uploads/{chat_id}/manifest"
            response = requests.get(manifest_url, timeout=5)
            response.raise_for_status()
            remote_manifest = response.json()
        except Exception as e:
            print(f"Failed to get manifest from file-server: {e}")
            remote_manifest = {}

        synced_count = 0
        skipped_count = 0
        error_count = 0
        details = []

        for file_info in files:
            temp_file_path = None  # Track temporary file for cleanup
            try:
                # Extract path from nested structure: file_info["file"]["path"]
                source_path = file_info.get("file", {}).get("path") if isinstance(file_info.get("file"), dict) else file_info.get("path")
                filename = file_info.get("name") or (os.path.basename(source_path) if source_path else "unknown")

                # Sanitize filename (prevent path traversal)
                filename = os.path.basename(filename)

                if not source_path:
                    details.append({"file": filename, "status": "error", "reason": "Source path not provided"})
                    error_count += 1
                    continue

                # Use OpenWebUI Storage provider - works universally for local, S3, GCS, Azure
                # For local files: returns path as-is
                # For cloud storage: downloads to temp dir and returns local path
                try:
                    from open_webui.storage.provider import Storage

                    if self.valves.DEBUG_LOGGING:
                        print(f"[SYNC] Resolving file path: {source_path}")

                    # Get local file path (download if needed)
                    local_file_path = Storage.get_file(source_path)

                    # Track if we downloaded from cloud (for cleanup)
                    if local_file_path != source_path:
                        temp_file_path = local_file_path
                        if self.valves.DEBUG_LOGGING:
                            print(f"[SYNC] Downloaded from cloud to: {temp_file_path}")

                    source_path = local_file_path

                except Exception as e:
                    if self.valves.DEBUG_LOGGING:
                        print(f"[SYNC] Failed to resolve file path: {e}")
                    details.append({"file": filename, "status": "error", "reason": f"File resolution failed: {str(e)}"})
                    error_count += 1
                    continue

                # Verify local file exists
                if not os.path.exists(source_path):
                    details.append({"file": filename, "status": "error", "reason": f"File not found at path: {source_path}"})
                    error_count += 1
                    continue

                # Calculate local MD5 checksum
                md5_hash = hashlib.md5()
                with open(source_path, "rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        md5_hash.update(chunk)
                local_md5 = md5_hash.hexdigest()

                # Check if file already exists with same checksum
                if remote_manifest.get(filename) == local_md5:
                    details.append({"file": filename, "status": "skipped", "reason": "Already synced"})
                    skipped_count += 1
                    continue

                # Upload file to file-server
                upload_url = f"{self.valves.FILE_SERVER_URL}/api/uploads/{chat_id}/{filename}"

                with open(source_path, "rb") as f:
                    files_data = {"file": (filename, f, "application/octet-stream")}
                    response = requests.post(upload_url, files=files_data, timeout=30)
                    response.raise_for_status()

                result = response.json()
                details.append({
                    "file": filename,
                    "status": "synced",
                    "size": result.get("size"),
                    "md5": result.get("md5")
                })
                synced_count += 1
                if self.valves.DEBUG_LOGGING:
                    print(f"[SYNC] Synced file: {filename} (md5: {local_md5})")

            except Exception as e:
                details.append({"file": filename, "status": "error", "reason": str(e)})
                error_count += 1
                if self.valves.DEBUG_LOGGING:
                    print(f"[SYNC] Failed to sync file {filename}: {e}")

            finally:
                # Cleanup temporary file if it was downloaded from cloud storage
                if temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                        if self.valves.DEBUG_LOGGING:
                            print(f"[SYNC] Cleaned up temporary file: {temp_file_path}")
                    except Exception as cleanup_error:
                        if self.valves.DEBUG_LOGGING:
                            print(f"[SYNC] Failed to cleanup temporary file: {cleanup_error}")

        if self.valves.DEBUG_LOGGING:
            print(f"[SYNC] Sync complete - synced: {synced_count}, skipped: {skipped_count}, errors: {error_count}")

        return {
            "synced": synced_count,
            "skipped": skipped_count,
            "errors": error_count,
            "details": details
        }

    def get_or_create_container(self, chat_id: str, extra_env: Optional[dict] = None):
        """
        Get existing container or create new one for this chat.

        Logic:
        1. Check if container with name owui-chat-{chat_id} exists
        2. If exists and stopped → start it
        3. If exists and running → use it
        4. If doesn't exist → create and start new one

        Args:
            chat_id: Chat ID for container naming
            extra_env: Optional dict of environment variables (GITLAB_TOKEN, GIT_AUTHOR_NAME, etc.)
        """
        # Lightweight SSH connection check before operation
        if not self._ensure_connection():
            print("[SSH-CHECK] Failed to establish connection in get_or_create_container()")
            raise Exception("SSH connection failed")

        if not self.client:
            raise Exception("Docker client not initialized")

        # Sanitize chat_id for Docker container naming
        # Docker names: [a-zA-Z0-9][a-zA-Z0-9_.-]+
        sanitized_id = re.sub(r'[^a-zA-Z0-9_.-]', '-', chat_id)
        container_name = f"owui-chat-{sanitized_id}"

        def _get_container():
            """Internal function to get container - wrapped for retry"""
            try:
                # Try to get existing container with retry on SSH errors
                container = self._execute_with_retry(self.client.containers.get, container_name)

                # Check status (reload also needs retry protection)
                self._execute_with_retry(container.reload)

                if container.status == "exited":
                    # Container exists but stopped → start it
                    self._execute_with_retry(container.start)
                    print(f"Started existing container: {container_name}")
                elif container.status == "running":
                    # Container already running → use it
                    print(f"Reusing running container: {container_name}")
                else:
                    # Container in other state (created, paused, etc.) → start it
                    self._execute_with_retry(container.start)
                    print(f"Started container in state '{container.status}': {container_name}")

                return container

            except docker.errors.NotFound:
                # Container doesn't exist → create new one
                print(f"Creating new container: {container_name}")
                return self.create_container(chat_id, container_name, extra_env)

        return _get_container()

    def create_container(self, chat_id: str, container_name: str, extra_env: Optional[dict] = None):
        """
        Create a new persistent container for this chat.

        Args:
            chat_id: Chat ID for naming and data paths
            container_name: Docker container name
            extra_env: Optional dict of environment variables
        """

        # Persistent workspace volume for this chat (code, npm packages, etc.)
        workspace_volume = f"chat-{chat_id}-workspace"

        # Host paths for user data (on Docker host, not local K8s pod!)
        # Host: /base_path/{chat_id}/uploads, /base_path/{chat_id}/outputs
        # Container: /mnt/user-data/uploads, /mnt/user-data/outputs
        chat_data_path = os.path.join(self.valves.USER_DATA_BASE_PATH, chat_id)
        uploads_path = os.path.join(chat_data_path, "uploads")
        outputs_path = os.path.join(chat_data_path, "outputs")

        # Create directories on Docker host (remote or local) with correct permissions
        # We use a temporary container to create directories on the host filesystem
        # This works for both SSH and local Docker connections
        try:
            print(f"Creating directories on Docker host: {uploads_path}, {outputs_path}")

            # Run temporary container to create directories with correct permissions
            # Mount parent directory and create subdirectories inside
            self._execute_with_retry(
                self.client.containers.run,
                image=self.valves.DOCKER_IMAGE,
                command=f"bash -c 'mkdir -p {shlex.quote(uploads_path)} {shlex.quote(outputs_path)} && chmod -R 777 {shlex.quote(chat_data_path)}'",
                volumes={
                    "/tmp": {"bind": "/tmp", "mode": "rw"}  # Mount /tmp to access computer-use-data
                },
                remove=True,  # Auto-remove container after execution
                detach=False,  # Wait for completion
                user="root"  # Run as root to ensure permissions are set
            )
            print(f"Directories created with 777 permissions: {chat_data_path}")
        except Exception as e:
            print(f"Warning: Failed to create directories via Docker: {e}")
            print(f"Docker will create them automatically with default permissions")

        print(f"User data path: {chat_data_path}")

        config = {
            "image": self.valves.DOCKER_IMAGE,
            "name": container_name,
            "hostname": f"chat-{chat_id[:8]}",

            # Keep container running with signal handling for proper shutdown
            # First run entrypoint to configure git/glab with GITLAB_TOKEN if present
            # trap: handle SIGTERM/SIGINT from shutdown timer
            # tail -f: keep container running
            # wait: allow trap to catch signals (wait is interruptible)
            "command": ["bash", "-c", "/home/assistant/.entrypoint.sh bash -c 'trap \"exit 0\" SIGTERM SIGINT; tail -f /dev/null & wait $!'"],
            "detach": True,
            "stdin_open": True,
            "tty": True,

            # Resource limits
            "mem_limit": self.valves.CONTAINER_MEM_LIMIT,
            "nano_cpus": int(self.valves.CONTAINER_CPU_LIMIT * 1_000_000_000),

            # Working directory (AI assistant workspace)
            "working_dir": "/home/assistant",

            # Run as non-root user (assistant has sudo access if needed)
            "user": "assistant:assistant",

            # Environment variables
            "environment": self._build_container_env(extra_env),

            # Volumes:
            # 1. workspace volume for /home/assistant (code, npm, etc)
            # 2. bind mount for /mnt/user-data (uploads/outputs accessible from host)
            # uploads: read-only (user uploads files, AI reads them)
            # outputs: read-write (AI generates files for user)
            "volumes": {
                workspace_volume: {"bind": "/home/assistant", "mode": "rw"},
                uploads_path: {"bind": "/mnt/user-data/uploads", "mode": "ro"},
                outputs_path: {"bind": "/mnt/user-data/outputs", "mode": "rw"},
            },

            # Labels for identification
            "labels": {
                "managed-by": "openwebui",
                "chat-id": chat_id,
                "tool": "computer-use-tools"
            },

            # Security
            "security_opt": ["no-new-privileges:true"],
        }

        # Network configuration
        if not self.valves.ENABLE_NETWORK:
            config["network_disabled"] = True

        # Create and start container with retry on SSH errors
        container = self._execute_with_retry(self.client.containers.create, **config)
        self._execute_with_retry(container.start)

        print(f"Created and started new container: {container_name}")
        return container

    def execute_bash(self, container, command: str, timeout: int = None) -> dict:
        """Execute bash command in container with timeout

        Args:
            container: Docker container instance
            command: Bash command to execute
            timeout: Optional custom timeout in seconds. If None, uses COMMAND_TIMEOUT valve.
        """
        # Lightweight SSH connection check before operation
        if not self._ensure_connection():
            print("[SSH-CHECK] Failed to establish connection in execute_bash()")
            return {
                "exit_code": -1,
                "output": "SSH connection failed",
                "success": False
            }

        # Reset auto-shutdown timer before executing command
        self._reset_shutdown_timer(container)

        try:
            # Wrap command with timeout to prevent hanging
            cmd_timeout = timeout if timeout is not None else self.valves.COMMAND_TIMEOUT
            timed_command = f"timeout {cmd_timeout} bash -c {shlex.quote(command)}"

            exec_result = container.exec_run(
                cmd=["bash", "-c", timed_command],
                stdout=True,
                stderr=True,
                demux=True,
                workdir="/home/assistant"
            )

            stdout_data, stderr_data = exec_result.output if exec_result.output else (b"", b"")

            # Decode with error handling for non-UTF8 output (e.g., binary files)
            stdout = stdout_data.decode("utf-8", errors="replace") if stdout_data else ""
            stderr = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

            output = ""
            if stdout:
                output += stdout
            if stderr:
                if output:
                    output += "\n"
                output += stderr

            # Exit code 124 = timeout command timed out
            if exec_result.exit_code == 124:
                output += f"\n[Command timed out after {cmd_timeout} seconds]"

            return {
                "exit_code": exec_result.exit_code,
                "output": output,
                "success": exec_result.exit_code == 0
            }

        except Exception as e:
            # Log SSH errors specifically for diagnostics
            error_msg = str(e).lower()
            if "ssh session not active" in error_msg or "connection" in error_msg:
                print(f"[SSH-ERROR] exec_run failed in execute_bash(): {e}")

            return {
                "exit_code": -1,
                "output": f"Execution error: {str(e)}",
                "success": False
            }

    def _reset_shutdown_timer(self, container):
        """
        Reset container auto-shutdown timer.
        Container will stop itself after IDLE_TIMEOUT seconds of inactivity.

        Uses a background sleep process that kills PID 1 (main process) when timer expires.
        Each command execution resets the timer by killing the previous sleep and starting a new one.
        """
        timeout = self.valves.CONTAINER_IDLE_TIMEOUT

        # Kill previous shutdown timer if it exists (including child processes)
        container.exec_run(
            "bash -c 'PID=$(cat /tmp/.shutdown-timer-pid 2>/dev/null); [ -n \"$PID\" ] && pkill -P $PID 2>/dev/null; kill $PID 2>/dev/null; true'",
            detach=False,
            user="assistant"
        )

        # Start new shutdown timer in background
        # When timer expires: kill PID 1 (tail -f /dev/null) -> container stops
        timer_cmd = f"bash -c 'echo $$ > /tmp/.shutdown-timer-pid && sleep {timeout} && kill 1'"
        container.exec_run(timer_cmd, detach=True, user="assistant")

    def execute_python_with_stdin(self, container, script: str, data: str) -> dict:
        """
        Execute Python script in container with data passed through stdin.
        This avoids 'argument list too long' error when dealing with large data.

        Args:
            container: Docker container instance
            script: Python script code to execute
            data: Data to pass to script via stdin (usually JSON)

        Returns:
            Dict with exit_code, output, and success status
        """
        import socket as sock_module

        # Lightweight SSH connection check before operation
        if not self._ensure_connection():
            print("[SSH-CHECK] Failed to establish connection in execute_python_with_stdin()")
            return {
                "exit_code": -1,
                "output": "SSH connection failed",
                "success": False
            }

        # Reset auto-shutdown timer before executing command
        self._reset_shutdown_timer(container)

        try:
            timeout = self.valves.COMMAND_TIMEOUT

            # Create exec instance with stdin enabled
            exec_id = container.client.api.exec_create(
                container.id,
                ["timeout", str(timeout), "python3", "-c", script],
                stdin=True,
                stdout=True,
                stderr=True,
                workdir="/home/assistant",
                user="assistant"
            )['Id']

            # Start execution with socket for stdin/stdout/stderr
            sock = container.client.api.exec_start(exec_id, socket=True)

            # Send data to stdin and close write end
            # Handle both local Docker (unix socket) and SSH (paramiko Channel)
            data_bytes = data.encode('utf-8')

            if hasattr(sock, '_sock'):
                # Local Docker (unix socket) - use ._sock
                sock._sock.sendall(data_bytes)
                sock._sock.shutdown(sock_module.SHUT_WR)
            else:
                # SSH connection (paramiko Channel) - use direct methods
                sock.sendall(data_bytes)
                if hasattr(sock, 'shutdown_write'):
                    sock.shutdown_write()

            # Read and demux output using official docker-py utilities
            gen = frames_iter(sock, tty=False)
            gen = (demux_adaptor(*frame) for frame in gen)
            stdout_data, stderr_data = consume_socket_output(gen, demux=True)

            sock.close()

            # Get exit code
            exec_info = container.client.api.exec_inspect(exec_id)
            exit_code = exec_info['ExitCode']

            # Decode output
            stdout = stdout_data.decode("utf-8", errors="replace") if stdout_data else ""
            stderr = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

            output = ""
            if stdout:
                output += stdout
            if stderr:
                if output:
                    output += "\n"
                output += stderr

            # Exit code 124 = timeout command timed out
            if exit_code == 124:
                output += f"\n[Command timed out after {timeout} seconds]"

            return {
                "exit_code": exit_code,
                "output": output,
                "success": exit_code == 0
            }

        except Exception as e:
            # Log SSH errors specifically for diagnostics
            error_msg = str(e).lower()
            if "ssh session not active" in error_msg or "connection" in error_msg:
                print(f"[SSH-ERROR] Low-level API failed in execute_python_with_stdin(): {e}")

            return {
                "exit_code": -1,
                "output": f"Execution error: {str(e)}",
                "success": False
            }


class _ToolsHelper:
    """
    Internal helper class for Tools utility methods.
    Not exposed to OpenWebUI API.
    """

    def __init__(self, valves, container_manager: _ContainerManager):
        self.valves = valves
        self.container_manager = container_manager

    async def build_extra_env(self, valves, __user__: Optional[dict] = None) -> dict:
        """
        Build environment variables dict for container.
        Takes valves as parameter to always use current values (avoids stale reference).

        Args:
            valves: Current valves object from Tools class
            __user__: User info dict with email and name

        Returns:
            Dict of environment variables to pass to container
        """
        user_email = __user__.get("email") if __user__ else None
        user_name = __user__.get("name") if __user__ else None
        gitlab_token = await _fetch_gitlab_token(
            user_email,
            valves.MCP_TOKENS_URL,
            valves.MCP_TOKENS_API_KEY
        ) if user_email else None

        extra_env = {
            "GITLAB_HOST": valves.GITLAB_HOST,
            "ANTHROPIC_API_KEY": valves.ANTHROPIC_API_KEY,
            "ANTHROPIC_BASE_URL": valves.ANTHROPIC_BASE_URL,
        }
        if gitlab_token:
            extra_env["GITLAB_TOKEN"] = gitlab_token
        if user_name:
            extra_env["GIT_AUTHOR_NAME"] = user_name
            extra_env["GIT_COMMITTER_NAME"] = user_name
        if user_email:
            extra_env["GIT_AUTHOR_EMAIL"] = user_email
            extra_env["GIT_COMMITTER_EMAIL"] = user_email
            # Header: Value format for ANTHROPIC_CUSTOM_HEADERS (lowercase for LiteLLM compatibility)
            extra_env["ANTHROPIC_CUSTOM_HEADERS"] = f"x-openwebui-user-email: {user_email}"

        return extra_env

    def format_elapsed_time(self, seconds: int) -> str:
        """Format elapsed time as human-readable string (e.g., '45s', '2m 15s')."""
        if seconds < 60:
            return f"{seconds}s"
        minutes = seconds // 60
        remaining_seconds = seconds % 60
        if remaining_seconds == 0:
            return f"{minutes}m"
        return f"{minutes}m {remaining_seconds}s"

    async def run_with_heartbeat(
        self,
        coro,
        __event_emitter__: Callable[[dict], Awaitable[None]],
        operation_name: str,
        timeout_seconds: int,
        heartbeat_interval: int = 15
    ):
        """
        Run coroutine with periodic status heartbeat updates.

        Sends status updates every heartbeat_interval seconds to show the user
        that a long-running operation is still in progress.

        Args:
            coro: Coroutine to execute
            __event_emitter__: OpenWebUI event emitter function
            operation_name: Name to show in status (e.g., "Sub-agent (sonnet)")
            timeout_seconds: Maximum execution time (shown in status)
            heartbeat_interval: Seconds between heartbeat updates (default: 15)

        Returns:
            Result of the coroutine
        """
        start_time = time.time()
        heartbeat_task = None

        async def heartbeat():
            while True:
                await asyncio.sleep(heartbeat_interval)
                elapsed = int(time.time() - start_time)
                elapsed_str = self.format_elapsed_time(elapsed)
                timeout_str = self.format_elapsed_time(timeout_seconds)
                if __event_emitter__:
                    await __event_emitter__({
                        "type": "status",
                        "data": {
                            "description": f"{operation_name} working... {elapsed_str} / {timeout_str} max",
                            "status": "in_progress",
                            "done": False,
                        }
                    })

        try:
            if __event_emitter__:
                heartbeat_task = asyncio.create_task(heartbeat())
            result = await coro
            return result
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    def parse_last_action(self, lines: list) -> Optional[str]:
        """
        Parse JSONL lines from Claude session log and return last meaningful action.
        Returns whichever came last in the log (text or tool_use) to avoid showing
        stale text while a long tool is executing.

        Args:
            lines: List of JSONL lines from session log

        Returns:
            String describing last action or None if no action found
        """
        last_action = None

        # Scan all lines - whichever action appears last wins
        for line in lines:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if data.get("type") == "assistant":
                    content = data.get("message", {}).get("content", [])
                    # Process items in order - last one wins
                    for item in content:
                        if item.get("type") == "text":
                            text = item.get("text", "")[:80]
                            text = text.replace('\n', ' ').strip()
                            if text:
                                last_action = text
                        elif item.get("type") == "tool_use":
                            name = item.get("name", "unknown")
                            inp = item.get("input", {})
                            detail = self.get_tool_detail(name, inp)
                            if detail:
                                last_action = f"{name}: {detail}"
                            else:
                                last_action = f"Using {name}..."
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

        return last_action

    def get_tool_detail(self, name: str, inp: dict) -> Optional[str]:
        """Extract useful detail from tool input for status display."""
        try:
            # First check for description field (Claude fills this for our tools)
            desc = inp.get("description", "")
            if desc:
                return desc.replace('\n', ' ').strip()

            # Fallback to tool-specific extraction (full text, no truncation)
            if name == "Bash":
                cmd = inp.get("command", "")
                return cmd.replace('\n', ' ').strip() if cmd else None
            elif name == "WebSearch":
                return inp.get("query", "")
            elif name == "Write":
                path = inp.get("file_path", "")
                return path.split("/")[-1] if path else None  # Just filename
            elif name == "Read":
                path = inp.get("file_path", "")
                return path.split("/")[-1] if path else None
            elif name == "Edit":
                path = inp.get("file_path", "")
                return path.split("/")[-1] if path else None
            elif name == "Grep":
                pattern = inp.get("pattern", "")
                return f'"{pattern}"' if pattern else None
            elif name == "Glob":
                return inp.get("pattern", "")
            elif name == "TodoWrite":
                todos = inp.get("todos", [])
                in_progress = [t for t in todos if t.get("status") == "in_progress"]
                if in_progress:
                    return in_progress[0].get("content", "")
                return f"{len(todos)} tasks"
        except Exception:
            pass
        return None

    async def run_with_heartbeat_and_logs(
        self,
        coro,
        container,
        __event_emitter__: Callable[[dict], Awaitable[None]],
        model_name: str,
        timeout_seconds: int,
        heartbeat_interval: int = 15
    ):
        """
        Run coroutine with heartbeat that reads Claude session logs.

        Enhanced version of run_with_heartbeat that also reads the sub-agent's
        session logs to show what it's currently doing.

        Args:
            coro: Coroutine to execute
            container: Docker container to read logs from
            __event_emitter__: OpenWebUI event emitter function
            model_name: Model name to show in status (e.g., "Sonnet", "Opus")
            timeout_seconds: Maximum execution time
            heartbeat_interval: Seconds between heartbeat updates (default: 15)

        Returns:
            Result of the coroutine
        """
        start_time = time.time()
        heartbeat_task = None

        async def heartbeat():
            while True:
                await asyncio.sleep(heartbeat_interval)
                elapsed = int(time.time() - start_time)
                elapsed_str = self.format_elapsed_time(elapsed)
                timeout_str = self.format_elapsed_time(timeout_seconds)

                # Try to read last action from session logs
                last_action = None
                try:
                    # Find most recent .jsonl file and read last 15 lines
                    read_logs_cmd = "tail -15 $(ls -t ~/.claude/projects/-home-assistant/*.jsonl 2>/dev/null | head -1) 2>/dev/null"
                    log_result = await asyncio.to_thread(
                        self.container_manager.execute_bash, container, read_logs_cmd, 10
                    )

                    if log_result.get("success") and log_result.get("output"):
                        lines = log_result["output"].strip().split('\n')
                        last_action = self.parse_last_action(lines)
                except Exception:
                    pass  # Ignore log reading errors

                # Always show current action with full text (no duplicate suppression)
                if last_action:
                    description = f"{model_name}: {last_action} ({elapsed_str})"
                else:
                    description = f"{model_name} working... {elapsed_str} / {timeout_str} max"

                if __event_emitter__:
                    await __event_emitter__({
                        "type": "status",
                        "data": {
                            "description": description,
                            "status": "in_progress",
                            "done": False,
                        }
                    })

        try:
            if __event_emitter__:
                heartbeat_task = asyncio.create_task(heartbeat())
            result = await coro
            return result
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def setup_mcp_servers(self, container, request, user_email: str) -> None:
        """
        Configure MCP servers in container from OpenWebUI configuration.
        Creates ~/.mcp.json file that Claude Code reads automatically.

        Args:
            container: Docker container instance
            request: FastAPI request object with OpenWebUI config
            user_email: User email for headers
        """
        if not request:
            print("[MCP] No request object, skipping MCP setup")
            return

        try:
            # Get MCP servers from OpenWebUI config
            owui_servers = await _get_mcp_servers_for_user(request, user_email)

            if not owui_servers:
                print("[MCP] No MCP servers configured in OpenWebUI")
                return

            # Convert to Claude Code format
            claude_mcp = _convert_owui_mcp_to_claude_format(owui_servers, user_email)

            if not claude_mcp:
                print("[MCP] No compatible MCP servers found")
                return

            # Create ~/.mcp.json in container
            mcp_json = json.dumps({"mcpServers": claude_mcp}, indent=2)
            escaped_json = shlex.quote(mcp_json)

            write_cmd = f"echo {escaped_json} > ~/.mcp.json"

            await asyncio.to_thread(
                self.container_manager.execute_bash,
                container,
                write_cmd,
                30  # 30 seconds timeout
            )

            print(f"[MCP] Created ~/.mcp.json with {len(claude_mcp)} servers: {list(claude_mcp.keys())}")

        except Exception as e:
            print(f"[MCP] Error setting up servers: {e}")

    async def format_sub_agent_result(
        self,
        output: str,
        model: str,
        max_turns: int,
        duration: float,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None
    ) -> str:
        """Parse Claude JSON output and format result with session_id for resume capability."""
        response_text = ""
        cost = 0.0
        turns = 0
        is_error = False
        session_id = ""

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
                            session_id = parsed.get("session_id", "")
                            break
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            print(f"[SUB-AGENT] Failed to parse JSON output: {e}")

        if not response_text:
            response_text = output

        status = "error" if is_error else "success"

        if __event_emitter__:
            await __event_emitter__({
                "type": "status",
                "data": {
                    "description": f"Sub-agent completed ({turns} turns, ${cost:.4f})",
                    "status": "complete" if status == "success" else "error",
                    "done": True,
                }
            })

        # Include session_id if available (for resume capability)
        session_info = f" | **Session:** `{session_id}`" if session_id else ""
        resume_hint = f"\n\n> To resume this session, use `resume_session_id=\"{session_id}\"`" if session_id and turns >= max_turns else ""

        return f"""**Sub-Agent Completed**
**Model:** {model} | **Turns:** {turns}/{max_turns} | **Cost:** ${cost:.4f} | **Duration:** {duration:.1f}s{session_info}

{response_text}{resume_hint}"""


class Tools:
    class Valves(BaseModel):
        DOCKER_SOCKET: str = Field(
            default="unix://var/run/docker.sock",
            description="Docker socket path. If OpenWebUI runs in Docker, mount the socket as a volume. "
                        "Leave empty to use SSH connection."
        )
        DOCKER_SSH_HOST: str = Field(
            default="dockerai",
            description="SSH host alias from ~/.ssh/config (default: dockerai). "
                        "If set, DOCKER_SOCKET will be ignored and SSH connection will be used. "
                        "Leave empty to use local Docker socket. "
                        "Requires SSH config file and paramiko package."
        )
        DOCKER_SSH_USER: str = Field(
            default="docker-user",
            description="SSH username for remote Docker connection"
        )
        DOCKER_IMAGE: str = Field(
            default="computer-use:latest",
            description="Docker image for code execution. Uses the same image as main container "
                        "(Python, Node.js, Java, LibreOffice, FFmpeg, Playwright, and many other tools). "
                        "Build with: docker-compose build ai-computer-use"
        )
        CONTAINER_MEM_LIMIT: str = Field(
            default="2g",
            description="Memory limit per container (e.g., '1g', '2g', '512m')"
        )
        CONTAINER_CPU_LIMIT: float = Field(
            default=1.0,
            description="CPU limit per container (1.0 = 1 core)"
        )
        COMMAND_TIMEOUT: int = Field(
            default=600,
            description="Maximum command execution time in seconds (default 10 minutes). "
                        "Large repo clones may need this much time."
        )
        ENABLE_NETWORK: bool = Field(
            default=True,
            description="Enable network access in containers"
        )
        USER_DATA_BASE_PATH: str = Field(
            default="/tmp/computer-use-data",
            description="Base path on host for user data. Each chat gets {base_path}/{chat_id}/ directory"
        )
        FILE_SERVER_URL: str = Field(
            default="http://localhost:8081",
            description="File server URL for uploading files from OpenWebUI to containers"
        )
        CONTAINER_IDLE_TIMEOUT: int = Field(
            default=600,
            description="Container auto-shutdown timeout in seconds (default 10 minutes). "
                        "Container stops itself after this period of inactivity."
        )
        DEBUG_LOGGING: bool = Field(
            default=False,
            description="Enable verbose debug logging for file sync and tool operations. "
                        "Logs include [FILE-DEBUG] and [SYNC] prefixes."
        )
        MCP_TOKENS_URL: str = Field(
            default="",
            description="URL of MCP Tokens Wrapper service for fetching user GitLab tokens. "
                        "Optional - leave empty if not using centralized token management."
        )
        MCP_TOKENS_API_KEY: str = Field(
            default="",
            description="Internal API key for MCP Tokens Wrapper (X-Internal-Api-Key header). "
                        "Required for fetching decrypted GitLab tokens."
        )
        GITLAB_HOST: str = Field(
            default="gitlab.com",
            description="GitLab host for git/glab authentication"
        )
        ANTHROPIC_BASE_URL: str = Field(
            default="https://api.anthropic.com",
            description="Base URL for Anthropic API (Claude Code). "
                        "Used for sub_agent tool."
        )
        ANTHROPIC_API_KEY: str = Field(
            default="",
            description="Anthropic API key for Claude Code sub-agent. "
                        "Required for sub_agent tool to work."
        )
        SUB_AGENT_DEFAULT_MODEL: str = Field(
            default="sonnet",
            description="Default model for sub_agent tool: 'sonnet' or 'opus'"
        )
        SUB_AGENT_MAX_TURNS: int = Field(
            default=50,
            description="Default max turns for sub_agent tool. "
                        "Controls how many iterations the agent can perform. "
                        "50 is good for presentations (15+ slides), complex refactoring."
        )
        SUB_AGENT_TIMEOUT: int = Field(
            default=1800,
            description="Timeout in seconds for sub_agent tool execution (default 30 minutes). "
                        "Complex tasks like presentations or large refactors need this much time."
        )

    def __init__(self):
        self.valves = self.Valves()
        self.file_handler = True  # Enable file uploads support
        self.citation = True  # Enable citations

        # Connect to Docker (SSH or local socket)
        try:
            if self.valves.DOCKER_SSH_HOST and self.valves.DOCKER_SSH_USER:
                docker_url = f"ssh://{self.valves.DOCKER_SSH_USER}@{self.valves.DOCKER_SSH_HOST}"
                print(f"Connecting to Docker via SSH: {docker_url}")
                self.client = docker.DockerClient(base_url=docker_url)
            else:
                # Use local Docker socket
                self.client = docker.DockerClient(base_url=self.valves.DOCKER_SOCKET)

            # Test connection
            self.client.ping()
            print("Docker connection successful")
        except Exception as e:
            print(f"Failed to connect to Docker: {e}")
            self.client = None

        # Initialize container manager (helper not exposed to API)
        # Container manager handles all reconnection logic internally
        self.container_manager = _ContainerManager(
            self.client,
            self.valves,
            mcp_tokens_url=self.valves.MCP_TOKENS_URL,
            mcp_tokens_api_key=self.valves.MCP_TOKENS_API_KEY
        )

        # Initialize tools helper (helper methods not exposed to API)
        self.helper = _ToolsHelper(self.valves, self.container_manager)

    async def bash_tool(
        self,
        command: str,
        description: str,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __metadata__: dict = None,
        __user__: dict = None,
        __files__: Optional[List[dict]] = None,
    ) -> str:
        """
        Run a bash command in the container

        :param command: Bash command to run in container
        :param description: Why I'm running this command
        :return: Command output (stdout/stderr)
        """

        chat_id = (__metadata__.get("chat_id") if __metadata__ else None) or "default"

        # DEBUG: Log file upload parameters
        if self.valves.DEBUG_LOGGING:
            print(f"[FILE-DEBUG] bash_tool called for chat: {chat_id}")
            print(f"[FILE-DEBUG] __files__ parameter: {__files__}")
            print(f"[FILE-DEBUG] __files__ is None: {__files__ is None}")
            print(f"[FILE-DEBUG] __files__ length: {len(__files__) if __files__ else 0}")
            if __files__:
                print(f"[FILE-DEBUG] First file structure: {__files__[0] if len(__files__) > 0 else 'N/A'}")

        # Check if command references uploaded files directory
        # Only sync files on-demand when they're actually needed
        uploads_path = "/mnt/user-data/uploads"
        needs_files = uploads_path in command or "uploads/" in command
        if self.valves.DEBUG_LOGGING:
            print(f"[FILE-DEBUG] needs_files: {needs_files} (command contains uploads: {('uploads' in command)})")

        # Sync uploaded files if provided and needed
        if __files__ and needs_files:
            sync_result = await asyncio.to_thread(
                self.container_manager.sync_uploaded_files,
                chat_id,
                __files__
            )
            if sync_result["synced"] > 0:
                print(f"Synced {sync_result['synced']} file(s) to container")
            elif sync_result["skipped"] > 0:
                print(f"Skipped {sync_result['skipped']} file(s) - already synced")
        elif needs_files:
            available_files = await asyncio.to_thread(
                self.container_manager.discover_and_list_files,
                chat_id
            )
            if available_files:
                print(f"Found {len(available_files)} file(s) at {uploads_path}/")

        if __event_emitter__:
            await __event_emitter__({
                "type": "status",
                "data": {
                    "description": description if description else "Executing bash command...",
                    "status": "in_progress",
                    "done": False,
                }
            })

        try:
            extra_env = await self.helper.build_extra_env(self.valves, __user__)
            container = await self.container_manager.get_container(chat_id, extra_env)

            # Reset idle timeout timer
            await asyncio.to_thread(
                self.container_manager._reset_shutdown_timer,
                container
            )

            # Execute with heartbeat status updates for long-running commands
            result = await self.helper.run_with_heartbeat(
                asyncio.to_thread(
                    self.container_manager.execute_bash,
                    container,
                    command
                ),
                __event_emitter__,
                operation_name=description if description else "Bash command",
                timeout_seconds=self.valves.COMMAND_TIMEOUT
            )

            if __event_emitter__:
                status = "complete" if result["success"] else "error"
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": "Command completed" if result["success"] else f"Command failed (exit {result['exit_code']})",
                        "status": status,
                        "done": True,
                    }
                })

            return result["output"] if result["output"] else f"[Exit code: {result['exit_code']}]"

        except Exception as e:
            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": "Execution error",
                        "status": "error",
                        "done": True,
                    }
                })
            return f"Error: {str(e)}"

    async def str_replace(
        self,
        description: str,
        old_str: str,
        path: str,
        new_str: str = "",
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __metadata__: dict = None,
        __user__: dict = None,
        __files__: Optional[List[dict]] = None,
    ) -> str:
        """
        Replace a unique string in a file with another string. The string to replace must appear exactly once in the file.

        :param description: Why I'm making this edit
        :param old_str: String to replace (must be unique in file)
        :param new_str: String to replace with (empty to delete)
        :param path: Path to the file to edit
        :return: Success message or error
        """

        chat_id = (__metadata__.get("chat_id") if __metadata__ else None) or "default"

        # Validate that old_str and new_str are different
        if old_str == new_str:
            return "Error: old_str and new_str are identical. No changes would be made."

        # Check if path references uploaded files directory - only check when needed
        uploads_path = "/mnt/user-data/uploads"
        if uploads_path in path or path.startswith("uploads/"):
            # Discover files from file-server
            available_files = await asyncio.to_thread(
                self.container_manager.discover_and_list_files,
                chat_id
            )
            if available_files:
                print(f"Found {len(available_files)} file(s) at {uploads_path}/")

        if __event_emitter__:
            await __event_emitter__({
                "type": "status",
                "data": {
                    "description": description if description else f"Editing {path}...",
                    "status": "in_progress",
                    "done": False,
                }
            })

        try:
            extra_env = await self.helper.build_extra_env(self.valves, __user__)
            container = await self.container_manager.get_container(chat_id, extra_env)

            # Use Python script with JSON to safely pass parameters via stdin
            # This avoids 'argument list too long' error for large strings
            script = """
import sys
import json

try:
    # Read parameters from stdin
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

            result = await asyncio.to_thread(
                self.container_manager.execute_python_with_stdin,
                container,
                script,
                payload
            )

            if __event_emitter__:
                status = "complete" if result["success"] else "error"
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": "File edited" if result["success"] else "Edit failed",
                        "status": status,
                        "done": True,
                    }
                })

            return result["output"]

        except Exception as e:
            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": "Execution error",
                        "status": "error",
                        "done": True,
                    }
                })
            return f"Error: {str(e)}"

    async def create_file(
        self,
        description: str,
        file_text: str,
        path: str,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __metadata__: dict = None,
        __user__: dict = None,
        __files__: Optional[List[dict]] = None,
    ) -> str:
        """
        Create a new file with content in the container

        :param description: Why I'm creating this file. ALWAYS PROVIDE THIS PARAMETER FIRST.
        :param file_text: Content to write to the file. ALWAYS PROVIDE THIS PARAMETER SECOND.
        :param path: Path to the file to create. ALWAYS PROVIDE THIS PARAMETER LAST.
        :return: Success message or error
        """

        chat_id = (__metadata__.get("chat_id") if __metadata__ else None) or "default"

        if __event_emitter__:
            await __event_emitter__({
                "type": "status",
                "data": {
                    "description": description if description else f"Creating {path}...",
                    "status": "in_progress",
                    "done": False,
                }
            })

        try:
            extra_env = await self.helper.build_extra_env(self.valves, __user__)
            container = await self.container_manager.get_container(chat_id, extra_env)

            # Use Python script with JSON to safely pass parameters via stdin
            # This avoids 'argument list too long' error for large file content
            script = """
import sys
import json
import os

try:
    # Read parameters from stdin
    data = json.loads(sys.stdin.read())
    path = data['path']
    file_text = data['file_text']

    # Create parent directories
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Write file
    with open(path, 'w') as f:
        f.write(file_text)

    print(f"Successfully created {path}")
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)
"""

            payload = json.dumps({"path": path, "file_text": file_text})

            result = await asyncio.to_thread(
                self.container_manager.execute_python_with_stdin,
                container,
                script,
                payload
            )

            if __event_emitter__:
                status = "complete" if result["success"] else "error"
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": "File created" if result["success"] else "Creation failed",
                        "status": status,
                        "done": True,
                    }
                })

            return result["output"] if result["success"] else f"Error: {result['output']}"

        except Exception as e:
            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": "Execution error",
                        "status": "error",
                        "done": True,
                    }
                })
            return f"Error: {str(e)}"

    async def view(
        self,
        description: str,
        path: str,
        view_range: ViewRange = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __metadata__: dict = None,
        __user__: dict = None,
        __files__: Optional[List[dict]] = None,
    ) -> str:
        """
        Supports viewing text files and directory listings. Binary files are detected and rejected with instructions to read appropriate SKILL documentation.

Supported path types:
- Directories: Lists files and directories with details
- Text files: Displays numbered lines. You can optionally specify a view_range to see specific lines.
- Binary files (.xlsx, .docx, .pptx, .pdf, etc.): Returns error with instructions to read the appropriate SKILL.md file

Binary file detection:
- Excel (.xlsx, .xls): Directs to /mnt/skills/public/xlsx/SKILL.md
- Word (.docx): Directs to /mnt/skills/public/docx/SKILL.md
- PowerPoint (.pptx): Directs to /mnt/skills/public/pptx/SKILL.md
- PDF (.pdf): Directs to /mnt/skills/public/pdf/SKILL.md
- Archives (.zip, .tar, .gz): Suggests appropriate extraction commands
- Images (.jpg, .png, .gif, etc.): Notifies that it's a binary image file

Note: Files with non-UTF-8 encoding will display hex escapes (e.g. \\x84) for invalid bytes

        :param description: Why I need to view this
        :param path: Absolute path to file or directory, e.g. `/repo/file.py` or `/repo`.
        :param view_range: Optional line range for text files. Format: [start_line, end_line] where lines are indexed starting at 1. Use [start_line, -1] to view from start_line to the end of the file. When not provided, the entire file is displayed, truncating from the middle if it exceeds 16,000 characters (showing beginning and end).
        :return: File contents, directory listing, or error message directing to SKILL documentation for binary files
        """

        chat_id = (__metadata__.get("chat_id") if __metadata__ else None) or "default"

        # DEBUG: Log file upload parameters
        if self.valves.DEBUG_LOGGING:
            print(f"[FILE-DEBUG] view called for chat: {chat_id}, path: {path}")
            print(f"[FILE-DEBUG] __files__ parameter: {__files__}")
            print(f"[FILE-DEBUG] __files__ length: {len(__files__) if __files__ else 0}")

        # Check if path references uploaded files directory - only check when needed
        uploads_path = "/mnt/user-data/uploads"
        needs_files = uploads_path in path or path.startswith("uploads/")
        if self.valves.DEBUG_LOGGING:
            print(f"[FILE-DEBUG] needs_files: {needs_files}")

        if needs_files:
            # Sync uploaded files if provided and needed
            if __files__:
                sync_result = await asyncio.to_thread(
                    self.container_manager.sync_uploaded_files,
                    chat_id,
                    __files__
                )
                if sync_result["synced"] > 0:
                    print(f"Synced {sync_result['synced']} file(s) to container")
                elif sync_result["skipped"] > 0:
                    print(f"Skipped {sync_result['skipped']} file(s) - already synced")
            else:
                available_files = await asyncio.to_thread(
                    self.container_manager.discover_and_list_files,
                    chat_id
                )
                if available_files:
                    print(f"Found {len(available_files)} file(s) at {uploads_path}/")

        if __event_emitter__:
            await __event_emitter__({
                "type": "status",
                "data": {
                    "description": description if description else f"Reading {path}...",
                    "status": "in_progress",
                    "done": False,
                }
            })

        try:
            extra_env = await self.helper.build_extra_env(self.valves, __user__)
            container = await self.container_manager.get_container(chat_id, extra_env)

            # Reset idle timeout timer
            await asyncio.to_thread(
                self.container_manager._reset_shutdown_timer,
                container
            )

            # Check if file or directory, then cat or ls accordingly
            # Use shlex.quote to safely handle paths with special characters
            quoted_path = shlex.quote(path)

            # Detect binary file types and provide helpful error messages
            binary_file_hints = {
                '.xlsx': 'Excel spreadsheet. Read the SKILL instructions first:\n  view /mnt/skills/public/xlsx/SKILL.md',
                '.xls': 'Excel spreadsheet (old format). Read the SKILL instructions first:\n  view /mnt/skills/public/xlsx/SKILL.md',
                '.docx': 'Word document. Read the SKILL instructions first:\n  view /mnt/skills/public/docx/SKILL.md',
                '.pptx': 'PowerPoint presentation. Read the SKILL instructions first:\n  view /mnt/skills/public/pptx/SKILL.md',
                '.pdf': 'PDF document. Read the SKILL instructions first:\n  view /mnt/skills/public/pdf/SKILL.md',
                '.zip': 'ZIP archive. Use unzip to extract:\n  unzip -l {path}',
                '.tar': 'TAR archive. Use tar to list:\n  tar -tvf {path}',
                '.gz': 'Gzip compressed file. Use gunzip or tar:\n  gunzip -c {path} | head -n 100',
                '.jpg': 'JPEG image. This is a binary image file.',
                '.jpeg': 'JPEG image. This is a binary image file.',
                '.png': 'PNG image. This is a binary image file.',
                '.gif': 'GIF image. This is a binary image file.',
                '.bmp': 'Bitmap image. This is a binary image file.',
                '.tiff': 'TIFF image. This is a binary image file.',
                '.mp4': 'MP4 video. This is a binary video file.',
                '.avi': 'AVI video. This is a binary video file.',
                '.mkv': 'MKV video. This is a binary video file.',
            }

            # Check if file extension matches binary file types
            file_ext = None
            for ext in binary_file_hints.keys():
                if path.lower().endswith(ext):
                    file_ext = ext
                    break

            if file_ext:
                # For binary files, provide helpful error message
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
                # Handle view_range for text files
                if view_range:
                    start, end = view_range
                    if end == -1:
                        # From start to end of file
                        cat_command = f"sed -n '{start},$p' {quoted_path} | cat -n"
                    else:
                        # Specific range
                        cat_command = f"sed -n '{start},{end}p' {quoted_path} | cat -n"
                else:
                    # Full file with line numbers
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

            result = await asyncio.to_thread(
                self.container_manager.execute_bash,
                container,
                command
            )

            output = result["output"] if result["output"] else "Error: No output"

            # Truncate output if no view_range specified and output exceeds 16,000 characters
            if not view_range and len(output) > 16000:
                # Keep first 8,000 and last 8,000 characters
                truncation_msg = "\n\n... [File truncated - middle section omitted. Total length: {} characters. Use view_range parameter to see specific lines.] ...\n\n".format(len(output))
                output = output[:8000] + truncation_msg + output[-8000:]

            if __event_emitter__:
                status = "complete" if result["success"] else "error"
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": "Read complete" if result["success"] else "Read failed",
                        "status": status,
                        "done": True,
                    }
                })

            return output

        except Exception as e:
            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": "Execution error",
                        "status": "error",
                        "done": True,
                    }
                })
            return f"Error: {str(e)}"

    async def sub_agent(
        self,
        task: str,
        description: str,
        model: str = "sonnet",
        max_turns: int = 50,
        mode: str = "act",
        working_directory: str = "/home/assistant",
        resume_session_id: str = "",
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __metadata__: dict = None,
        __user__: dict = None,
        __files__: Optional[List[dict]] = None,
        __request__=None,  # FastAPI request for MCP config access
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

        IMPORTANT: Task must follow ROLE/DIRECTIVE/CONSTRAINTS/PROCESS/OUTPUT structure.
        See /mnt/skills/public/sub-agent/SKILL.md for task writing guidelines.

        :param task: Structured task with ROLE, DIRECTIVE, CONSTRAINTS, PROCESS, OUTPUT sections
        :param description: Why you are delegating this task to a sub-agent
        :param model: AI model - "sonnet" (fast, default) or "opus" (powerful)
        :param max_turns: Max iterations, default 50. Use 30 for simple tasks, 100 for complex
        :param mode: "act" (execute, default) or "plan" (plan only)
        :param working_directory: Work dir, default /home/assistant
        :param resume_session_id: Session ID to resume (from previous result)
        :return: Sub-agent's response with task results, cost, turn count, and session_id
        """
        chat_id = (__metadata__.get("chat_id") if __metadata__ else None) or "default"
        user_email = __user__.get("email") if __user__ else None

        # Validate parameters
        if model not in ["sonnet", "opus"]:
            model = "sonnet"
        if max_turns <= 0:
            max_turns = 50
        if mode not in ["act", "plan"]:
            mode = "act"

        # Always use bypassPermissions for autonomous sub-agent (no interactive terminal)
        # mode="plan" affects agent behavior via system prompt, not Claude CLI permissions
        permission_mode = "bypassPermissions"
        mode_label = "planning" if mode == "plan" else "executing"

        if __event_emitter__:
            await __event_emitter__({
                "type": "status",
                "data": {
                    "description": description if description else f"Starting sub-agent ({model}, {mode_label})...",
                    "status": "in_progress",
                    "done": False,
                }
            })

        # Sync uploaded files if provided
        if __files__:
            sync_result = await asyncio.to_thread(
                self.container_manager.sync_uploaded_files,
                chat_id,
                __files__
            )
            if sync_result["synced"] > 0:
                print(f"[SUB-AGENT] Synced {sync_result['synced']} file(s) to container")

        try:
            extra_env = await self.helper.build_extra_env(self.valves, __user__)
            container = await self.container_manager.get_container(chat_id, extra_env)

            # === Setup MCP servers from OpenWebUI config ===
            await self.helper.setup_mcp_servers(container, __request__, user_email or "")

            # === Plan file for task persistence across context compaction ===
            plan_file = "/home/assistant/task_plan.md"
            file_base_url = f"{self.valves.FILE_SERVER_URL}/files/{chat_id}"

            # ANTHROPIC_CUSTOM_HEADERS passes x-openwebui-user-email header for LiteLLM tracking
            headers_env = ""
            if user_email:
                headers_env = f"ANTHROPIC_CUSTOM_HEADERS={shlex.quote(f'x-openwebui-user-email: {user_email}')} "

            # Common flags for both new and resume sessions
            # --disallowedTools: AskUserQuestion and ExitPlanMode don't work in headless mode
            base_flags = (
                f"--max-turns {max_turns} "
                f"--permission-mode {permission_mode} "
                f"--disallowedTools 'AskUserQuestion,ExitPlanMode' "
                f"--output-format json"
            )

            if resume_session_id:
                # === RESUME SESSION ===
                # Don't modify task_plan.md - original plan stays intact
                # Pass new instructions in prompt - caller knows context and writes meaningful continuation
                # Sub-agent can re-read original plan from file if context was compacted
                resume_prompt = f"Your original task is saved at {plan_file}. Read it if your context was compacted. New instruction from caller: {task}"
                escaped_prompt = shlex.quote(resume_prompt)

                claude_command = (
                    f"cd {shlex.quote(working_directory)} && "
                    f"{headers_env}"
                    f"claude -p {escaped_prompt} "
                    f"--resume {shlex.quote(resume_session_id)} "
                    f"{base_flags}"
                )
            else:
                # === NEW SESSION ===
                # Save task to plan file - source of truth for sub-agent
                write_plan_cmd = f"cat > {plan_file} << 'TASK_PLAN_EOF'\n{task}\nTASK_PLAN_EOF"
                await asyncio.to_thread(
                    self.container_manager.execute_bash,
                    container,
                    write_plan_cmd,
                    30000  # 30s timeout for file write
                )

                # Build system prompt with environment info and skills
                system_prompt = f"""<critical_instruction>
Your task plan is saved at {plan_file}

BEFORE ANY ACTION:
1. Read {plan_file} to understand your full task
2. If context becomes compacted, re-read {plan_file} - it is your source of truth
3. The plan file contains all details you need

Never forget: {plan_file} has your complete instructions.
</critical_instruction>

<environment>
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

                # Short prompt - full details in plan file
                short_task = f"Read and execute your task plan from {plan_file}"
                escaped_task = shlex.quote(short_task)
                escaped_system = shlex.quote(system_prompt)

                claude_command = (
                    f"cd {shlex.quote(working_directory)} && "
                    f"{headers_env}"
                    f"claude -p {escaped_task} "
                    f"--model {model} "
                    f"--append-system-prompt {escaped_system} "
                    f"{base_flags}"
                )

            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": description if description else f"Sub-agent {mode_label} ({model}, max {max_turns} turns)...",
                        "status": "in_progress",
                        "done": False,
                    }
                })

            start_time = time.time()

            # Execute with extended timeout and heartbeat that reads session logs
            result = await self.helper.run_with_heartbeat_and_logs(
                asyncio.to_thread(
                    self.container_manager.execute_bash,
                    container,
                    claude_command,
                    self.valves.SUB_AGENT_TIMEOUT
                ),
                container,
                __event_emitter__,
                model_name=model.capitalize(),  # "Sonnet" or "Opus"
                timeout_seconds=self.valves.SUB_AGENT_TIMEOUT
            )

            duration = time.time() - start_time
            output = result.get("output", "")

            # Parse JSON result and format response
            return await self.helper.format_sub_agent_result(output, model, max_turns, duration, __event_emitter__)

        except Exception as e:
            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": "Sub-agent execution error",
                        "status": "error",
                        "done": True,
                    }
                })
            return f"Sub-agent error: {str(e)}"

