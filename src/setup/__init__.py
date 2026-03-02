"""
Open Brain Setup Wizard - Interactive first-run configuration
"""

import os
import yaml
from pathlib import Path
from typing import Dict, Any


def prompt_env(var_name: str, default: str = "", secret: bool = False) -> str:
    """Prompt for environment variable."""
    if secret:
        import getpass
        return getpass.getpass(f"{var_name}: ") or default
    return input(f"{var_name} [{default}]: ") or default


def run_setup() -> Dict[str, Any]:
    """Run interactive setup wizard."""
    print("""
╔═══════════════════════════════════════════════════════════════╗
║              Open Brain Setup Wizard v1.0                 ║
║                                                           ║
║  Your personal semantic memory system                     ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    
    config = {}
    
    # ═══════════════════════════════════════════════════════
    # 1. DATABASE CONFIGURATION
    # ═══════════════════════════════════════════════════════
    print("\n📀 DATABASE CONFIGURATION")
    print("─" * 50)
    config['database'] = {
        'host': prompt_env("DB_HOST", "postgres"),
        'port': int(prompt_env("DB_PORT", "5432")),
        'name': prompt_env("DB_NAME", "openbrain"),
        'user': prompt_env("DB_USER", "postgres"),
        'password': prompt_env("DB_PASSWORD", "", secret=True) or "openbrain",
    }
    
    # ═══════════════════════════════════════════════════════
    # 2. EMBEDDING PROVIDER
    # ═══════════════════════════════════════════════════════
    print("\n🧠 EMBEDDING PROVIDER")
    print("─" * 50)
    print("Choose your embedding provider:")
    print("  1. OpenRouter  - FREE, no setup (recommended)")
    print("  2. OpenAI      - GPT embeddings")
    print("  3. Ollama      - Local embeddings")
    print("  4. Custom      - Any OpenAI-compatible API")
    
    choice = input("\nChoice [1]: ") or "1"
    
    embedder_config = {}
    
    if choice == "1":  # OpenRouter
        embedder_config['provider'] = 'openrouter'
        embedder_config['model'] = prompt_env("EMBED_MODEL", "text-embedding-3-small")
        embedder_config['dimensions'] = 768
        api_key = prompt_env("OPENROUTER_API_KEY", "", secret=True)
        if api_key:
            os.environ['OPENROUTER_API_KEY'] = api_key
            
    elif choice == "2":  # OpenAI
        embedder_config['provider'] = 'openai'
        embedder_config['model'] = prompt_env("EMBED_MODEL", "text-embedding-3-small")
        embedder_config['dimensions'] = 1536
        api_key = prompt_env("OPENAI_API_KEY", "", secret=True)
        if api_key:
            os.environ['OPENAI_API_KEY'] = api_key
            
    elif choice == "3":  # Ollama
        embedder_config['provider'] = 'ollama'
        embedder_config['model'] = prompt_env("EMBED_MODEL", "nomic-embed-text")
        embedder_config['dimensions'] = 768
        embedder_config['ollama_base_url'] = prompt_env("OLLAMA_BASE_URL", "http://localhost:11434")
        
    elif choice == "4":  # Custom
        embedder_config['provider'] = 'custom'
        embedder_config['model'] = prompt_env("EMBED_MODEL", "text-embedding-ada-002")
        embedder_config['dimensions'] = 1536
        embedder_config['custom_base_url'] = prompt_env("CUSTOM_API_URL", "https://api.openai.com/v1")
        api_key = prompt_env("CUSTOM_API_KEY", "", secret=True)
        if api_key:
            os.environ['CUSTOM_API_KEY'] = api_key
    
    config['embedder'] = embedder_config
    
    # ═══════════════════════════════════════════════════════
    # 3. LLM PROVIDER (for MCP server)
    # ═══════════════════════════════════════════════════════
    print("\n🤖 LLM PROVIDER (for MCP)")
    print("─" * 50)
    print("Choose LLM for the MCP server:")
    print("  1. OpenRouter - FREE tier available")
    print("  2. OpenAI     - GPT-4, GPT-4o")
    print("  3. Anthropic  - Claude models")
    print("  4. Ollama     - Local models")
    print("  5. MiniMax    - Fast & cheap")
    
    llm_choice = input("\nChoice [1]: ") or "1"
    
    llm_config = {}
    
    if llm_choice == "1":
        llm_config['provider'] = 'openrouter'
        llm_config['model'] = prompt_env("LLM_MODEL", "openai/gpt-4o-mini")
        api_key = os.environ.get('OPENROUTER_API_KEY') or prompt_env("OPENROUTER_API_KEY", "", secret=True)
        if api_key:
            os.environ['OPENROUTER_API_KEY'] = api_key
            
    elif llm_choice == "2":
        llm_config['provider'] = 'openai'
        llm_config['model'] = prompt_env("LLM_MODEL", "gpt-4o-mini")
        api_key = prompt_env("OPENAI_API_KEY", "", secret=True)
        if api_key:
            os.environ['OPENAI_API_KEY'] = api_key
            
    elif llm_choice == "3":
        llm_config['provider'] = 'anthropic'
        llm_config['model'] = prompt_env("LLM_MODEL", "claude-3-haiku-20240307")
        api_key = prompt_env("ANTHROPIC_API_KEY", "", secret=True)
        if api_key:
            os.environ['ANTHROPIC_API_KEY'] = api_key
            
    elif llm_choice == "4":
        llm_config['provider'] = 'ollama'
        llm_config['model'] = prompt_env("LLM_MODEL", "llama3")
        llm_config['base_url'] = prompt_env("OLLAMA_BASE_URL", "http://localhost:11434")
        
    elif llm_choice == "5":
        llm_config['provider'] = 'minimax'
        llm_config['model'] = prompt_env("LLM_MODEL", "MiniMax-M2.1")
        api_key = prompt_env("MINIMAX_API_KEY", "", secret=True)
        if api_key:
            os.environ['MINIMAX_API_KEY'] = api_key
    
    config['llm'] = llm_config
    
    # ═══════════════════════════════════════════════════════
    # 4. API SERVER
    # ═══════════════════════════════════════════════════════
    print("\n🌐 API SERVER")
    print("─" * 50)
    config['api'] = {
        'host': '0.0.0.0',
        'port': int(prompt_env("API_PORT", "8000")),
        'cors_origins': ['*'],
    }
    
    config['mcp'] = {
        'host': '0.0.0.0',
        'port': int(prompt_env("MCP_PORT", "8080")),
    }
    
    config['dashboard'] = {
        'port': int(prompt_env("DASHBOARD_PORT", "8501")),
    }
    
    # ═══════════════════════════════════════════════════════
    # 5. SECURITY
    # ═══════════════════════════════════════════════════════
    print("\n🔒 SECURITY")
    print("─" * 50)
    config['security'] = {
        'mode': prompt_env("SECURITY_MODE", "direct"),
    }
    
    # ═══════════════════════════════════════════════════════
    # 6. NOTIFICATIONS (Optional)
    # ═══════════════════════════════════════════════════════
    print("\n📬 NOTIFICATIONS (Optional)")
    print("─" * 50)
    
    analytics = {'notifications': {}}
    
    if input("Enable Telegram? [y/N]: ").lower() == 'y':
        analytics['notifications']['telegram'] = {
            'enabled': True,
            'bot_token': prompt_env("TELEGRAM_BOT_TOKEN", ""),
            'chat_id': prompt_env("TELEGRAM_CHAT_ID", ""),
        }
    
    if input("Enable Email? [y/N]: ").lower() == 'y':
        analytics['notifications']['email'] = {
            'enabled': True,
            'smtp_host': prompt_env("SMTP_HOST", "smtp.gmail.com"),
            'smtp_port': int(prompt_env("SMTP_PORT", "587")),
            'smtp_user': prompt_env("SMTP_USER", ""),
            'smtp_password': prompt_env("SMTP_PASSWORD", "", secret=True),
            'from_email': prompt_env("EMAIL_FROM", ""),
        }
    
    config['analytics'] = analytics
    
    # Tags config
    config['tags'] = {
        'deny_list': ['password', 'secret', 'api_key', 'token', 'credential'],
        'default_tags': ['auto'],
    }
    
    # ═══════════════════════════════════════════════════════
    # SAVE CONFIGURATION
    # ═══════════════════════════════════════════════════════
    print("\n💾 SAVING CONFIGURATION")
    print("─" * 50)
    
    # Save to config/settings.yaml
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(config_path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    
    print(f"✓ Configuration saved to: {config_path}")
    
    # Save .env file
    env_path = Path(__file__).parent.parent / ".env"
    env_vars = []
    for key in ['OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 
                'MINIMAX_API_KEY', 'CUSTOM_API_KEY', 'OLLAMA_BASE_URL',
                'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'SMTP_HOST', 
                'SMTP_PORT', 'SMTP_USER', 'SMTP_PASSWORD', 'EMAIL_FROM']:
        value = os.environ.get(key)
        if value:
            env_vars.append(f"{key}={value}")
    
    if env_vars:
        with open(env_path, 'w') as f:
            f.write("# Open Brain Environment Variables\n")
            f.write("# Copy this to .env and fill in remaining values\n\n")
            for key in ['DB_HOST', 'DB_PORT', 'DB_NAME', 'DB_USER', 'DB_PASSWORD',
                       'API_PORT', 'MCP_PORT', 'DASHBOARD_PORT', 'SECURITY_MODE']:
                f.write(f"{key}={config.get(key.split('_')[0].lower(), {}).get(key, '') if key in ['API_PORT', 'MCP_PORT', 'DASHBOARD_PORT'] else ''}\n")
            f.write("\n# API Keys\n")
            f.write("\n".join(env_vars))
        
        print(f"✓ Environment variables saved to: {env_path}")
    
    print("""
╔═══════════════════════════════════════════════════════════════╗
║                   SETUP COMPLETE!                         ║
║                                                           ║
║  Next steps:                                              ║
║  1. Review config/settings.yaml                         ║
║  2. docker compose up -d                                ║
║  3. Visit http://localhost:8501                        ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    
    return config


if __name__ == "__main__":
    run_setup()
