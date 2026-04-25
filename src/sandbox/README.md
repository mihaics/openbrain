# OpenSandbox Integration

This module provides secure command execution by running untrusted commands in isolated Docker containers using [OpenSandbox](https://github.com/alibaba/OpenSandbox).

## Why Use Sandboxing?

| Without Sandbox | With Sandbox |
|----------------|--------------|
| Commands run on host | Commands run in container |
| Can access all files | Files isolated |
| Can exfiltrate data | Network can be blocked |
| Can kill processes | Process isolated |

## Quick Start

### 1. Start OpenSandbox Server

```bash
# Install
uv pip install opensandbox-server

# Initialize config
opensandbox-server init-config ~/.sandbox.toml --example docker

# Start server (runs in background)
opensandbox-server &
```

### 2. Check Server Status

```bash
python -m src.sandbox.cli check
# ✓ OpenSandbox server is running
```

### 3. Execute Commands

```bash
# Direct (default - no sandbox)
python -m src.sandbox.cli run "echo hello"

# Sandboxed
python -m src.sandbox.cli run "echo hello" --sandbox

# With timeout
python -m src.sandbox.cli run "sleep 10" --timeout 5
```

## Python API

```python
import asyncio
from src.sandbox import SandboxExecutor, DirectExecutor, get_executor

async def example():
    # Direct execution (default)
    direct = DirectExecutor()
    result = await direct.run("ls -la")
    print(result.stdout)
    
    # Sandboxed execution
    sandbox = SandboxExecutor()
    result = await sandbox.run("pip install requests")
    print(result.stdout)
    
    # Using convenience function
    from src.sandbox import exec_sandbox
    result = await exec_sandbox("echo 'sandboxed!'")

asyncio.run(example())
```

## Configuration

Edit `config/settings.yaml`:

```yaml
security:
  mode: sandbox  # or "direct"
  
  sandbox:
    server_url: http://localhost:8080
    image: opensandbox/code-interpreter:v1.0.1
    timeout_seconds: 300
    
    # Mount workspace (optional)
    # workspace_mount: /path/to/workspace
    
    # Network
    network:
      allow_egress: true
      block_egress: false
```

## Security Modes

### Ephemeral (Default)
- Container created, command runs, container destroyed
- No files persist
- Fastest

### Persistent (With Mount)
- Workspace directory mounted into container
- Files persist after container dies
- Good for development

## Example: Safe Package Installation

```python
# DON'T do this directly - uses system pip
# await exec_direct("pip install untrusted-package")

# DO this - sandboxed
result = await exec_sandbox("pip install untrusted-package")

# Even if package is malicious, it can't:
# - Access your files
# - Exfiltrate data
# - See your environment variables
```

## CLI Options

```bash
# Check if sandbox server is running
python -m src.sandbox.cli check

# Preferred project CLI
openbrain exec "ls -la"
openbrain exec --sandbox --timeout 120 "python -V"

# Run command directly (no sandbox)
python -m src.sandbox.cli run "ls -la"
python -m src.sandbox.cli run "pip install requests" --timeout 120

# Run command in sandbox
python -m src.sandbox.cli run "ls -la" --sandbox
python -m src.sandbox.cli run "curl http://evil.com" --sandbox

# Run Python code
python -m src.sandbox.cli python "print(1+1)"
python -m src.sandbox.cli python "import os; print(os.listdir('/'))" --sandbox
```

## Troubleshooting

### Server not starting
```bash
# Check Docker is running
docker version

# Check logs
opensandbox-server  # runs in foreground
```

### Connection refused
```bash
# Server must be running
opensandbox-server &

# Check health
curl http://localhost:8080/health
```

### Timeout
```bash
# Increase timeout
python -m src.sandbox.cli run "make" --timeout 600
```
