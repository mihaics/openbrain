# Open Brain - Security Options

Open Brain supports running untrusted operations in an **OpenSandbox** isolated environment for enhanced security.

## Security Modes

| Mode | Isolation | Use Case |
|------|-----------|-----------|
| **direct** (default) | None | Trusted operations, local development |
| **sandbox** | Docker container | Untrusted code, external prompts |

## Configuration

```yaml
# config/settings.yaml
security:
  # Default mode for exec operations
  mode: direct  # or "sandbox"
  
  # OpenSandbox settings
  sandbox:
    # Server URL (default: local)
    server_url: http://localhost:8080
    
    # Sandbox image
    image: opensandbox/code-interpreter:v1.0.1
    
    # Timeout for sandbox operations
    timeout_seconds: 300
    
    # Workspace mount (optional - mount host directory)
    # Leave empty for ephemeral-only (no persistence)
    workspace_mount: /home/tom/.openbrain/workspace
    
    # Network policy
    network:
      # Allow external network access
      allow_egress: true
      # Allowed ports (if restrict_egress is true)
      allowed_ports: [80, 443]
      # Block all egress
      block_egress: false
```

## Usage

### Via CLI

```bash
# Direct execution (default)
openbrain exec "pip install requests"

# Sandboxed execution
openbrain exec --sandbox "pip install requests"

# Sandboxed with custom timeout
openbrain exec --sandbox --timeout 600 "make install"
```

### Via API

```bash
# Direct
curl -X POST http://localhost:8000/exec \
  -d '{"command": "pip install requests"}'

# Sandboxed
curl -X POST http://localhost:8000/exec \
  -d '{"command": "pip install requests", "sandbox": true}'
```

### Via MCP

```json
{
  "name": "exec_command",
  "arguments": {
    "command": "pip install requests",
    "sandbox": true,
    "timeout": 300
  }
}
```

## Sandbox Modes

### Ephemeral (No Mount)
```python
sandbox = await Sandbox.create(
    "opensandbox/code-interpreter:v1.0.1",
    timeout=timedelta(minutes=5)
)
# Files are destroyed when sandbox ends
```

### Persistent (With Mount)
```python
sandbox = await Sandbox.create(
    "opensandbox/code-interpreter:v1.0.1",
    volumes=[
        Volume(
            container_path="/workspace",
            host_path="/home/tom/.openbrain/workspace"
        )
    ],
    timeout=timedelta(minutes=10)
)
# Changes persist in host directory
```

## Security Comparison

| Aspect | Direct Mode | Sandbox Mode |
|--------|-------------|--------------|
| File Access | Host filesystem | Ephemeral or mounted |
| Network | Full access | Configurable |
| Process | Host processes | Isolated |
| Escape Risk | N/A | Low (Docker) |
| Persistence | Yes | Optional |

## Starting OpenSandbox Server

```bash
# Install
uv pip install opensandbox-server

# Initialize config
opensandbox-server init-config ~/.sandbox.toml --example docker

# Start server
opensandbox-server

# Verify
curl http://localhost:8080/health
```

## Best Practices

1. **Default to sandbox** for any external/untrusted input
2. **Use ephemeral mode** for one-off operations
3. **Mount workspace only** when you need file persistence
4. **Block egress** for sensitive operations
5. **Set timeouts** to prevent runaway processes

## Examples

### Safe Code Execution

```bash
# Run untrusted Python code safely
openbrain exec --sandbox << 'EOF'
import os
print(os.listdir("/"))  # Only sees sandbox fs
EOF
```

### Web Scraping

```bash
# Scraping with sandbox - can block if needed
openbrain exec --sandbox --allow-network true "curl https://example.com"
```

### File Operations

```bash
# Mount workspace for persistent file access
openbrain exec --sandbox --mount /path/to/workspace "ls -la"
```

## Troubleshooting

### Sandbox won't start
```bash
# Check Docker is running
docker version

# Check server logs
opensandbox-server  # runs in foreground
```

### Timeout errors
```yaml
# Increase timeout in config
security:
  sandbox:
    timeout_seconds: 600  # 10 minutes
```

### Network blocked
```yaml
# Allow network access
security:
  sandbox:
    network:
      allow_egress: true
```
