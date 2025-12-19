import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from mitmproxy import http
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster


class RequestLogger:
    """Mitmproxy addon that logs HTTP requests and responses to individual files."""

    def __init__(self, log_dir):
        """Initialize with a log directory."""
        self.log_dir = Path(log_dir)
        self.request_counter = 0

    def _get_sanitized_path(self, url):
        """Extract and sanitize URL path for filename."""
        from urllib.parse import urlparse

        parsed_url = urlparse(url)
        url_path = parsed_url.path.strip("/")

        # Replace slashes with underscores and sanitize
        url_path = url_path.replace("/", "_")
        url_path = "".join(
            c if c.isalnum() or c in ("_", "-") else "_" for c in url_path
        )

        # Limit to 50 characters
        if len(url_path) > 50:
            url_path = url_path[:50]

        # Use a default if path is empty
        if not url_path:
            url_path = "root"

        return url_path

    def stream_chunk(self, chunk: bytes) -> bytes:
        # Process chunk here
        return chunk

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if "text/event-stream" in flow.response.headers.get("Content-Type", ""):
            flow.response.stream = self.stream_chunk

    def response(self, flow: http.HTTPFlow) -> None:
        """Log complete HTTP request/response pair to individual file in JSONL format."""
        self.request_counter += 1

        # Create unique filename
        url_path = self._get_sanitized_path(flow.request.url)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{timestamp}_{self.request_counter:03d}_{url_path}_{flow.request.method}.jsonl"
        file_path = self.log_dir / filename

        # Write request and response to file in JSONL format
        with open(file_path, "w") as f:
            req = flow.request
            resp = flow.response

            # Build request JSON object
            request_obj = {
                "method": req.method,
                "url": req.url,
                "host": req.host,
                "port": req.port,
                "headers": dict(req.headers),
            }

            # Handle request body
            if req.content:
                try:
                    request_obj["body"] = json.loads(req.content.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    try:
                        request_obj["body"] = req.content.decode(
                            "utf-8", errors="replace"
                        )
                    except Exception:
                        import base64

                        request_obj["body"] = base64.b64encode(req.content).decode(
                            "ascii"
                        )
                        request_obj["body_encoding"] = "base64"
            else:
                request_obj["body"] = None

            # Write request line
            f.write(json.dumps(request_obj) + "\n")

            # Build response JSON object
            if resp:
                response_obj = {
                    "status_code": resp.status_code,
                    "reason": resp.reason,
                    "headers": dict(resp.headers),
                }

                # Handle response body
                if resp.content:
                    try:
                        response_obj["body"] = json.loads(resp.content.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        try:
                            response_obj["body"] = resp.content.decode(
                                "utf-8", errors="replace"
                            )
                        except Exception:
                            import base64

                            response_obj["body"] = base64.b64encode(
                                resp.content
                            ).decode("ascii")
                            response_obj["body_encoding"] = "base64"
                else:
                    response_obj["body"] = None
            else:
                response_obj = {"error": "No response received"}

            # Write response line
            f.write(json.dumps(response_obj) + "\n")


async def run_proxy(port: int, log_dir: str, upstream: str):
    """Run the mitmproxy server in reverse proxy mode."""
    opts = Options(
        listen_host="0.0.0.0",
        listen_port=port,
        mode=["reverse:" + upstream],
    )
    master = DumpMaster(
        opts,
        with_termlog=False,
        with_dumper=False,
    )
    master.addons.add(RequestLogger(log_dir))

    try:
        await master.run()
        print("Proxy stopped", file=sys.stderr)
    except KeyboardInterrupt:
        master.shutdown()


def start_proxy_thread(port: int, log_dir: str, upstream: str):
    """Start the proxy in a separate thread."""

    def run_async_proxy():
        asyncio.run(run_proxy(port, log_dir, upstream))

    proxy_thread = threading.Thread(target=run_async_proxy, daemon=True)
    proxy_thread.start()
    return proxy_thread


def run_command_with_proxy(command: list[str], proxy_port: int):
    """Run a command with proxy environment variables set."""
    # Set up environment with proxy configuration
    env = os.environ.copy()
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    env["ANTHROPIC_BASE_URL"] = proxy_url

    # Run the command with piped stdio
    process = subprocess.Popen(
        command, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, env=env
    )

    return process.wait()


def main():
    """Main entry point."""
    # Check if command is provided
    if len(sys.argv) < 2:
        print("Usage: python main.py <command> [args...]", file=sys.stderr)
        print("Example: python main.py mycli --option value", file=sys.stderr)
        print("\nEnvironment variables:", file=sys.stderr)
        print(
            "  LLMSPY_UPSTREAM - Upstream target for reverse proxy mode (e.g., https://api.example.com)",
            file=sys.stderr,
        )
        sys.exit(1)

    # Extract command and arguments
    command = sys.argv[1:]
    proxy_port = 8080

    # Create timestamped log directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = f"llm-requests-{timestamp}"
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    # Check for reverse proxy mode
    upstream = os.environ.get("LLMSPY_UPSTREAM")
    if not upstream:
        raise ValueError("LLMSPY_UPSTREAM environment variable is not set")

    print(
        f"Starting reverse proxy on port {proxy_port} -> {upstream}...",
        file=sys.stderr,
    )

    # Start the proxy server
    _proxy_thread = start_proxy_thread(proxy_port, log_dir, upstream)

    # Give the proxy a moment to start
    time.sleep(2)

    print(f"Running command: {' '.join(command)}", file=sys.stderr)
    print(f"HTTP requests will be logged to: {log_dir}/\n", file=sys.stderr)

    # Run the command with proxy configured
    try:
        exit_code = run_command_with_proxy(command, proxy_port)
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except FileNotFoundError:
        print(f"Error: Command '{command[0]}' not found", file=sys.stderr)
        sys.exit(127)


if __name__ == "__main__":
    main()
