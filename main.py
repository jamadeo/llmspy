import asyncio
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

    def response(self, flow: http.HTTPFlow) -> None:
        """Log complete HTTP request/response pair to individual file."""
        self.request_counter += 1

        # Create unique filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{timestamp}_{self.request_counter:03d}_{flow.request.method}.log"
        file_path = self.log_dir / filename

        # Write request and response to file
        with open(file_path, "w") as f:
            req = flow.request
            resp = flow.response

            # Log request
            print(f"{'=' * 80}", file=f)
            print(f"REQUEST", file=f)
            print(f"{'=' * 80}", file=f)
            print(f"Method: {req.method}", file=f)
            print(f"URL: {req.url}", file=f)
            print(f"Host: {req.host}:{req.port}", file=f)
            print(f"\nRequest Headers:", file=f)
            for name, value in req.headers.items():
                print(f"  {name}: {value}", file=f)

            if req.content:
                print(f"\nRequest Body ({len(req.content)} bytes):", file=f)
                print("-" * 80, file=f)
                try:
                    body_text = req.content.decode("utf-8", errors="replace")
                    print(body_text, file=f)
                except Exception as e:
                    print(f"[Error decoding body: {e}]", file=f)
                    print(f"Body (first 200 bytes): {req.content[:200]!r}", file=f)

            # Log response
            if resp:
                print(f"\n{'=' * 80}", file=f)
                print(f"RESPONSE", file=f)
                print(f"{'=' * 80}", file=f)
                print(f"Status: {resp.status_code} {resp.reason}", file=f)
                print(f"\nResponse Headers:", file=f)
                for name, value in resp.headers.items():
                    print(f"  {name}: {value}", file=f)

                if resp.content:
                    print(f"\nResponse Body ({len(resp.content)} bytes):", file=f)
                    print("-" * 80, file=f)
                    try:
                        body_text = resp.content.decode("utf-8", errors="replace")
                        print(body_text, file=f)
                    except Exception as e:
                        print(f"[Error decoding body: {e}]", file=f)
                        print(f"Body (first 200 bytes): {resp.content[:200]!r}", file=f)
            else:
                print(f"\n{'=' * 80}", file=f)
                print(f"WARNING: No response received", file=f)

            print(f"\n{'=' * 80}", file=f)


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
    proxy_thread = start_proxy_thread(proxy_port, log_dir, upstream)

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
