"""
Source connectors for importing data into Open Brain.
"""
__all__ = [
    'TelegramConnector',
    'WhatsAppConnector',
    'ClaudeCodeConnector',
    'GmailConnector',
    'FileWatcherConnector'
]

_CONNECTORS = {
    'TelegramConnector': ('.telegram', 'TelegramConnector'),
    'WhatsAppConnector': ('.whatsapp', 'WhatsAppConnector'),
    'ClaudeCodeConnector': ('.claude_code', 'ClaudeCodeConnector'),
    'GmailConnector': ('.gmail', 'GmailConnector'),
    'FileWatcherConnector': ('.file_watcher', 'FileWatcherConnector'),
}


def __getattr__(name):
    """Load optional connectors only when they are requested."""
    if name not in _CONNECTORS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    module_name, attr_name = _CONNECTORS[name]
    value = getattr(import_module(module_name, __name__), attr_name)
    globals()[name] = value
    return value
