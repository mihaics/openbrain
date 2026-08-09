"""
Multi-provider embedder for Open Brain.

Supports:
- openrouter: OpenRouter API (free tier available)
- openai: OpenAI API
- ollama: Local Ollama
- custom: Any OpenAI-compatible API

Configure in settings.yaml:
```yaml
embedder:
  provider: openrouter  # openrouter, openai, ollama, custom
  model: nomic-embed-text  # or openrouter model
  
  # OpenRouter (default - free tier available)
  openrouter_api_key: ${OPENROUTER_API_KEY}
  
  # OpenAI (optional)
  openai_api_key: ${OPENAI_API_KEY}
  
  # Ollama (optional)
  ollama_base_url: http://localhost:11434
  
  # Custom API (optional)
  custom_base_url: ${CUSTOM_API_URL}
  custom_api_key: ${CUSTOM_API_KEY}
  
  dimensions: 768
```
"""
import os
import sys
from typing import List, Optional
from abc import ABC, abstractmethod

import requests
import yaml


class EmbedderConfig:
    """Configuration for embedder with multiple provider support."""
    
    _instance: Optional['EmbedderConfig'] = None
    
    def __init__(self, config_path: str = None):
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(__file__),
                '..', '..', 'config', 'settings.yaml'
            )
        
        # Load config
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        embedder_cfg = config.get('embedder', {})
        
        # Provider settings
        self.provider = embedder_cfg.get('provider', 'openrouter')
        
        # Model
        self.model = embedder_cfg.get('model', 'nomic-embed-text')
        
        # Dimensions
        self.dimensions = embedder_cfg.get('dimensions', 768)
        
        # OpenRouter (default)
        self.openrouter_api_key = os.environ.get(
            'OPENROUTER_API_KEY',
            embedder_cfg.get('openrouter_api_key', '')
        )
        
        # OpenAI
        self.openai_api_key = os.environ.get(
            'OPENAI_API_KEY',
            embedder_cfg.get('openai_api_key', '')
        )
        
        # Ollama
        self.ollama_base_url = embedder_cfg.get(
            'ollama_base_url',
            os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434')
        )
        
        # Custom (any OpenAI-compatible API)
        self.custom_base_url = os.environ.get(
            'CUSTOM_API_URL',
            embedder_cfg.get('custom_base_url', '')
        )
        self.custom_api_key = os.environ.get(
            'CUSTOM_API_KEY',
            embedder_cfg.get('custom_api_key', '')
        )
    
    @classmethod
    def get_instance(cls, config_path: str = None) -> 'EmbedderConfig':
        if cls._instance is None:
            cls._instance = cls(config_path)
        return cls._instance
    
    @classmethod
    def reset(cls):
        """Reset singleton (useful for testing)."""
        cls._instance = None


class BaseEmbedder(ABC):
    """Abstract base class for embedders."""
    
    @abstractmethod
    def embed(self, text: str) -> List[float]:
        """Generate embedding for a single text."""
        pass
    
    @abstractmethod
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts."""
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if the provider is available."""
        pass
    
    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Embedding dimensions."""
        pass


class OpenRouterEmbedder(BaseEmbedder):
    """OpenRouter API embedder (default, free tier available)."""
    
    # Default free models on OpenRouter
    DEFAULT_MODELS = {
        'text-embedding-3-small': 1536,
        'text-embedding-ada-002': 1536,
    }
    
    def __init__(self, config: EmbedderConfig):
        self.config = config
        self.api_key = config.openrouter_api_key
        self.model = config.model or 'text-embedding-3-small'
        self.base_url = 'https://openrouter.ai/api/v1'
        
        # Get dimensions for model
        self._dimensions = config.dimensions
        if self.model in self.DEFAULT_MODELS:
            self._dimensions = self.DEFAULT_MODELS[self.model]
    
    @property
    def dimensions(self) -> int:
        return self._dimensions
    
    def embed(self, text: str) -> List[float]:
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'HTTP-Referer': 'https://openbrain.local',  # Required by OpenRouter
            'X-Title': 'Open Brain',
        }
        
        response = requests.post(
            f'{self.base_url}/embeddings',
            json={
                'model': self.model,
                'input': text,
            },
            headers=headers,
            timeout=30
        )
        response.raise_for_status()
        
        result = response.json()
        return result['data'][0]['embedding']
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'HTTP-Referer': 'https://openbrain.local',
            'X-Title': 'Open Brain',
        }
        
        response = requests.post(
            f'{self.base_url}/embeddings',
            json={
                'model': self.model,
                'input': texts,
            },
            headers=headers,
            timeout=60
        )
        response.raise_for_status()
        
        result = response.json()
        # Sort by index to maintain order
        embeddings = sorted(result['data'], key=lambda x: x['index'])
        return [e['embedding'] for e in embeddings]
    
    def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            response = requests.get(
                f'{self.base_url}/models',
                headers={'Authorization': f'Bearer {self.api_key}'},
                timeout=10
            )
            return response.status_code == 200
        except Exception:
            return False


class OpenAIEmbedder(BaseEmbedder):
    """OpenAI API embedder."""
    
    DEFAULT_MODELS = {
        'text-embedding-3-small': 1536,
        'text-embedding-3-large': 3072,
        'text-embedding-ada-002': 1536,
    }
    
    def __init__(self, config: EmbedderConfig):
        self.config = config
        self.api_key = config.openai_api_key
        self.model = config.model or 'text-embedding-3-small'
        self.base_url = os.environ.get(
            'OPENAI_BASE_URL',
            'https://api.openai.com/v1'
        )
        
        self._dimensions = config.dimensions
        if self.model in self.DEFAULT_MODELS:
            self._dimensions = self.DEFAULT_MODELS[self.model]
    
    @property
    def dimensions(self) -> int:
        return self._dimensions
    
    def embed(self, text: str) -> List[float]:
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        
        response = requests.post(
            f'{self.base_url}/embeddings',
            json={
                'model': self.model,
                'input': text,
            },
            headers=headers,
            timeout=30
        )
        response.raise_for_status()
        
        result = response.json()
        return result['data'][0]['embedding']
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        
        response = requests.post(
            f'{self.base_url}/embeddings',
            json={
                'model': self.model,
                'input': texts,
            },
            headers=headers,
            timeout=60
        )
        response.raise_for_status()
        
        result = response.json()
        embeddings = sorted(result['data'], key=lambda x: x['index'])
        return [e['embedding'] for e in embeddings]
    
    def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            response = requests.get(
                f'{self.base_url}/models',
                headers={'Authorization': f'Bearer {self.api_key}'},
                timeout=10
            )
            return response.status_code == 200
        except Exception:
            return False


class OllamaEmbedder(BaseEmbedder):
    """Local Ollama embedder."""
    
    def __init__(self, config: EmbedderConfig):
        self.config = config
        self.model = config.model or 'nomic-embed-text'
        self.base_url = config.ollama_base_url
        self.dims = config.dimensions
    
    @property
    def dimensions(self) -> int:
        return self.dims
    
    def embed(self, text: str) -> List[float]:
        response = requests.post(
            f'{self.base_url}/api/embeddings',
            json={
                'model': self.model,
                'prompt': text
            },
            timeout=30
        )
        response.raise_for_status()

        result = response.json()
        embedding = result.get('embedding')
        if not embedding:
            raise RuntimeError(
                f"Ollama returned empty embedding for model '{self.model}'. "
                f"Check that the model is pulled and serving."
            )
        return embedding
    
    def embed_batch(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Embed several texts, yielding None for any that fail.

        A failed item is None, never a zero vector. A zero vector is not a
        missing embedding: pgvector's cosine distance against it is NaN, so
        the row participates in ranking and duplicate detection with an
        undefined score instead of being correctly excluded.
        """
        embeddings: List[Optional[List[float]]] = []

        for text in texts:
            try:
                embeddings.append(self.embed(text))
            except Exception as e:
                # stderr, not stdout: src/main.py is an MCP stdio server and
                # anything on stdout corrupts the protocol stream.
                print(f"Error embedding text: {e}", file=sys.stderr)
                embeddings.append(None)

        return embeddings
    
    def is_available(self) -> bool:
        try:
            response = requests.get(f'{self.base_url}/api/tags', timeout=5)
            return response.status_code == 200
        except Exception:
            return False


class CustomEmbedder(BaseEmbedder):
    """Custom OpenAI-compatible API embedder."""
    
    def __init__(self, config: EmbedderConfig):
        self.config = config
        self.model = config.model or 'text-embedding-ada-002'
        self.base_url = config.custom_base_url
        self.api_key = config.custom_api_key
        self.dims = config.dimensions
    
    @property
    def dimensions(self) -> int:
        return self.dims
    
    def embed(self, text: str) -> List[float]:
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        
        response = requests.post(
            f'{self.base_url}/embeddings',
            json={
                'model': self.model,
                'input': text,
            },
            headers=headers,
            timeout=30
        )
        response.raise_for_status()
        
        result = response.json()
        return result['data'][0]['embedding']
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        
        response = requests.post(
            f'{self.base_url}/embeddings',
            json={
                'model': self.model,
                'input': texts,
            },
            headers=headers,
            timeout=60
        )
        response.raise_for_status()
        
        result = response.json()
        embeddings = sorted(result['data'], key=lambda x: x['index'])
        return [e['embedding'] for e in embeddings]
    
    def is_available(self) -> bool:
        if not self.base_url or not self.api_key:
            return False
        try:
            response = requests.get(
                f'{self.base_url}/models',
                headers={'Authorization': f'Bearer {self.api_key}'},
                timeout=10
            )
            return response.status_code == 200
        except Exception:
            return False


class EmbedderFactory:
    """Factory for creating embedders based on configuration."""
    
    @staticmethod
    def create(config: EmbedderConfig = None) -> BaseEmbedder:
        """Create an embedder based on config."""
        if config is None:
            config = EmbedderConfig.get_instance()
        
        provider = config.provider.lower()
        
        if provider == 'openrouter':
            return OpenRouterEmbedder(config)
        elif provider == 'openai':
            return OpenAIEmbedder(config)
        elif provider == 'ollama':
            return OllamaEmbedder(config)
        elif provider == 'custom':
            return CustomEmbedder(config)
        else:
            # Default to OpenRouter
            return OpenRouterEmbedder(config)


# Global embedder instance
_embedder: Optional[BaseEmbedder] = None


def get_embedder(config_path: str = None) -> BaseEmbedder:
    """Get the global embedder instance."""
    global _embedder
    
    if _embedder is None:
        if config_path:
            EmbedderConfig.reset()
        config = EmbedderConfig.get_instance(config_path)
        _embedder = EmbedderFactory.create(config)
    
    return _embedder


def create_embedding(text: str) -> List[float]:
    """Convenience function to create an embedding."""
    return get_embedder().embed(text)


class EmbeddingError(RuntimeError):
    """Raised when an embedding cannot be produced or is unusable.

    Callers treat this as "no embedding available" and degrade — storing a
    NULL embedding, or falling back to text-only search. It must never
    propagate to a user as a failed store: losing a memory is worse than
    storing one that is temporarily absent from vector search.
    """


def create_validated_embedding(
    text: str,
    expected_dim: Optional[int] = None,
) -> List[float]:
    """Create an embedding, rejecting output that would corrupt the database.

    Validation happens here, in the caller's try block, rather than in the
    database layer. The DB-layer check in queries._validate_embedding runs
    after the caller's exception handling has already completed, so relying
    on it alone turns a degraded embedding into a lost memory.

    Args:
        text: Content to embed.
        expected_dim: Declared dimension of the target column, normally
            get_vector_dim(). None skips the dimension check.

    Raises:
        EmbeddingError: provider failed, or returned an empty, all-zero, or
            wrongly-dimensioned vector.
    """
    try:
        embedding = create_embedding(text)
    except Exception as e:
        raise EmbeddingError(str(e)) from e

    if not embedding:
        raise EmbeddingError("embedder returned an empty embedding")

    if all(v == 0.0 for v in embedding):
        raise EmbeddingError(
            "embedder returned an all-zero embedding, which is not a valid "
            "vector for cosine similarity"
        )

    if expected_dim and len(embedding) != expected_dim:
        raise EmbeddingError(
            f"embedding dimension mismatch: got {len(embedding)}, "
            f"target column is vector({expected_dim})"
        )

    return embedding


def create_embeddings(texts: List[str]) -> List[Optional[List[float]]]:
    """Convenience function to create multiple embeddings.

    Failed items are None. Callers must handle that before inserting.
    """
    return get_embedder().embed_batch(texts)


# For backward compatibility
OllamaEmbedder = OllamaEmbedder
OllamaConfig = EmbedderConfig
