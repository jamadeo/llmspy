import asyncio
import os
import subprocess
import sys
import threading

from mitmproxy import http
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster


class RequestLogger:
    """Mitmproxy addon that logs HTTP requests and responses."""

    def __init__(self, log_file):
        """Initialize with a log file."""
        self.log_file = log_file

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        """Enable streaming for all flows."""
        flow.request.stream = True

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Enable streaming for responses and log headers early."""
        flow.response.stream = True

        import time

        timestamp = time.strftime("%H:%M:%S")

        # Log request when we see response headers (request body may have been streamed)
        req = flow.request
        print(f"\n{'=' * 80}", file=self.log_file)
        print(f"[{timestamp}] REQUEST: {req.method} {req.url}", file=self.log_file)
        print(f"Host: {req.host}:{req.port}", file=self.log_file)
        print(f"Headers:", file=self.log_file)
        for name, value in req.headers.items():
            print(f"  {name}: {value}", file=self.log_file)
        print("Body: <streamed>", file=self.log_file)
        print(f"{'=' * 80}\n", file=self.log_file)

        # Log response headers
        resp = flow.response
        print(f"\n{'=' * 80}", file=self.log_file)
        print(
            f"[{timestamp}] RESPONSE: {flow.request.method} {flow.request.url}",
            file=self.log_file,
        )
        print(f"Status: {resp.status_code} {resp.reason}", file=self.log_file)
        print(f"Headers:", file=self.log_file)
        for name, value in resp.headers.items():
            print(f"  {name}: {value}", file=self.log_file)
        print("Body: <streamed> - forwarding to client...", file=self.log_file)
        print(f"{'=' * 80}\n", file=self.log_file)
        self.log_file.flush()

    def request(self, flow: http.HTTPFlow) -> None:
        """Log HTTP request details (only called for non-streamed requests)."""
        req = flow.request
        print(f"\n{'=' * 80}", file=self.log_file)
        print(f"REQUEST: {req.method} {req.url}", file=self.log_file)
        print(f"Host: {req.host}:{req.port}", file=self.log_file)
        print(f"Headers:", file=self.log_file)
        for name, value in req.headers.items():
            print(f"  {name}: {value}", file=self.log_file)
        if req.content:
            print(f"Body length: {len(req.content)} bytes", file=self.log_file)
            body_preview = req.content[:100]
            print(
                f"Body preview (first 100 bytes): {body_preview!r}", file=self.log_file
            )
        print(f"{'=' * 80}\n", file=self.log_file)
        self.log_file.flush()

    def response(self, flow: http.HTTPFlow) -> None:
        """Log HTTP response details."""
        resp = flow.response
        if resp:
            print(f"\n{'=' * 80}", file=self.log_file)
            print(
                f"RESPONSE: {flow.request.method} {flow.request.url}",
                file=self.log_file,
            )
            print(f"Status: {resp.status_code} {resp.reason}", file=self.log_file)
            print(f"Headers:", file=self.log_file)
            for name, value in resp.headers.items():
                print(f"  {name}: {value}", file=self.log_file)
            if resp.content:
                print(f"Body length: {len(resp.content)} bytes", file=self.log_file)
                body_preview = resp.content[:100]
                print(
                    f"Body preview (first 100 bytes): {body_preview!r}",
                    file=self.log_file,
                )
            print(f"{'=' * 80}\n", file=self.log_file)
            self.log_file.flush()
        else:
            print(
                f"\nWARNING: No response for {flow.request.method} {flow.request.url}",
                file=self.log_file,
            )
            self.log_file.flush()


async def run_proxy(port: int, log_file, upstream: str):
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
    master.addons.add(RequestLogger(log_file))

    try:
        await master.run()
        print("Proxy stopped", file=sys.stderr)
    except KeyboardInterrupt:
        master.shutdown()


def start_proxy_thread(port: int, log_file, upstream: str):
    """Start the proxy in a separate thread."""

    def run_async_proxy():
        asyncio.run(run_proxy(port, log_file, upstream))

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
    log_filename = "http_requests.log"

    # Check for reverse proxy mode
    upstream = os.environ.get("LLMSPY_UPSTREAM")
    if not upstream:
        raise ValueError("LLMSPY_UPSTREAM environment variable is not set")

    print(
        f"Starting reverse proxy on port {proxy_port} -> {upstream}...",
        file=sys.stderr,
    )

    # Open log file
    log_file = open(log_filename, "w", buffering=1)

    # Start the proxy server
    proxy_thread = start_proxy_thread(proxy_port, log_file, upstream)

    # Give the proxy a moment to start
    import time

    time.sleep(2)

    print(f"Running command: {' '.join(command)}", file=sys.stderr)
    print(f"HTTP requests will be logged to: {log_filename}\n", file=sys.stderr)

    # Run the command with proxy configured
    try:
        exit_code = run_command_with_proxy(command, proxy_port)
        log_file.close()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        log_file.close()
        sys.exit(130)
    except FileNotFoundError:
        print(f"Error: Command '{command[0]}' not found", file=sys.stderr)
        log_file.close()
        sys.exit(127)


if __name__ == "__main__":
    main()
