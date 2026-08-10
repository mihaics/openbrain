"""
Entity extraction for Open Brain.
Uses basic NLP for named entity recognition.
"""
import re
from typing import Dict, List, Optional, Set, Tuple

import nltk
from nltk import pos_tag, word_tokenize
from nltk.chunk import ne_chunk
from nltk.tree import Tree


_NLTK_DATA = [
    ('tokenizers/punkt', 'punkt'),
    ('taggers/averaged_perceptron_tagger', 'averaged_perceptron_tagger'),
    ('chunkers/maxent_ne_chunker', 'maxent_ne_chunker'),
    ('corpora/words', 'words'),
]


def _ensure_nltk_data():
    """Download the corpora NER needs, tolerating failure.

    This runs at import time, and importing this module must never fail: a
    missing corpus only costs the NLTK half of extraction (the call site
    already degrades to pattern-based entities), while a raised exception
    takes down the MCP server, the API, and every connector with it. An
    offline machine hits exactly that path.
    """
    for resource, package in _NLTK_DATA:
        try:
            nltk.data.find(resource)
        except LookupError:
            try:
                nltk.download(package, quiet=True)
            except Exception:
                pass


_ensure_nltk_data()



# Ambiguous technology terms.
#
# tech_keywords held bare 'go', 'rest', 'api', 'ui', 'node' and matched them
# with \bgo\b against lowercased text, so the English verb "go" tagged a
# memory as the Go language: of 47 memories tagged `go`, only 27 contained
# the word at all.
#
# Two trigger kinds, deliberately distinct:
#   SUFFICIENT_ALIASES  - present => the technology is meant, bare token not
#                         required. `go1.24` lives here because \bgo\b does
#                         not match it at all ('1' is a word character), so
#                         requiring a bare token would make the rule dead.
#   QUALIFIED_CONTEXTS  - the bare token AND a context phrase must both appear.
SUFFICIENT_ALIASES = {
    "go": (r"\bgolang\b", r"\bgoroutines?\b", r"\bgo\.mod\b", r"\bgofmt\b",
           r"\bgo1\.\d+"),
    "nodejs": (r"\bnode\.js\b", r"\bnodejs\b", r"\bnpm\b", r"\bnode_modules\b"),
    "rest": (r"\brestful\b",),
    "java": (r"\bjvm\b", r"\bmaven\b", r"\bgradle\b", r"\bmvn\b"),
}

QUALIFIED_CONTEXTS = {
    "go": (r"\bgo mod\b", r"\bgo get\b", r"\bgo routine\b"),
    "rest": (r"\brest api\b", r"\brest endpoint\b"),
    "api": (r"\bapi key\b", r"\bapi endpoint\b", r"\bgraphql api\b",
            r"\brest api\b", r"\bapi call\b"),
    "ui": (r"\bui component\b", r"\bweb ui\b", r"\bui test\b"),
    "ux": (r"\bux design\b", r"\buser experience\b"),
    "node": (r"\bnode version\b",),
    "java": (r"\bjava \d+\b", r"\bspring boot\b"),
}

# One technology must not enter under two spellings: the keyword layer already
# canonicalises node -> nodejs, so entity extraction routes through the same map.
CANONICAL_TECH = {"node": "nodejs"}

AMBIGUOUS_TECH = set(QUALIFIED_CONTEXTS) | set(SUFFICIENT_ALIASES)


def _emit_tech(keyword: str, lowered: str) -> Optional[str]:
    """Decide whether a technology keyword is really meant, and under which name."""
    canonical = CANONICAL_TECH.get(keyword, keyword)

    for alias in SUFFICIENT_ALIASES.get(canonical, ()):
        if re.search(alias, lowered):
            return canonical

    if canonical in QUALIFIED_CONTEXTS or keyword in QUALIFIED_CONTEXTS:
        contexts = QUALIFIED_CONTEXTS.get(keyword, ()) + QUALIFIED_CONTEXTS.get(canonical, ())
        has_token = re.search(rf"\b{re.escape(keyword)}\b", lowered)
        if has_token and any(re.search(c, lowered) for c in contexts):
            return canonical
        return None

    return canonical


class EntityExtractor:
    """Extract entities from text using NLTK."""
    
    # Common entity patterns
    EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
    URL_PATTERN = re.compile(r'https?://[^\s]+')
    PHONE_PATTERN = re.compile(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b')
    DATE_PATTERN = re.compile(r'\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b')
    HASHTAG_PATTERN = re.compile(r'#\w+')
    MENTION_PATTERN = re.compile(r'@\w+')
    
    def __init__(self):
        self._initialize_entity_types()
    
    def _initialize_entity_types(self):
        """Initialize common entity keywords."""
        # Technology
        self.tech_keywords = {
            'python', 'javascript', 'java', 'rust', 'go', 'typescript',
            'react', 'vue', 'angular', 'node', 'docker', 'kubernetes',
            'aws', 'gcp', 'azure', 'postgresql', 'mysql', 'mongodb',
            'redis', 'elasticsearch', 'kafka', 'graphql', 'rest',
            'api', 'sdk', 'cli', 'ui', 'ux', 'html', 'css'
        }
        
        # People roles
        self.people_roles = {
            'ceo', 'cto', 'cfo', 'coo', 'president', 'founder',
            'developer', 'engineer', 'designer', 'manager', 'director',
            'vp', 'head', 'lead', 'senior', 'junior', 'intern'
        }
        
        # Project types
        self.project_keywords = {
            'project', 'feature', 'bug', 'task', 'issue', 'pr',
            'release', 'deploy', 'milestone', 'sprint', 'backlog'
        }
    
    def extract(self, text: str) -> Dict[str, List[str]]:
        """
        Extract all entities from text.
        
        Args:
            text: Input text
            
        Returns:
            Dictionary mapping entity types to lists of entities
        """
        entities: Dict[str, Set[str]] = {
            'emails': set(),
            'urls': set(),
            'phones': set(),
            'dates': set(),
            'hashtags': set(),
            'mentions': set(),
            'people': set(),
            'organizations': set(),
            'locations': set(),
            'technologies': set(),
            'projects': set()
        }
        
        # Pattern-based extraction
        entities['emails'].update(self.EMAIL_PATTERN.findall(text))
        entities['urls'].update(self.URL_PATTERN.findall(text))
        entities['phones'].update(self.PHONE_PATTERN.findall(text))
        entities['dates'].update(self.DATE_PATTERN.findall(text))
        entities['hashtags'].update(self.HASHTAG_PATTERN.findall(text))
        entities['mentions'].update(self.MENTION_PATTERN.findall(text))
        
        # NLTK-based NER
        try:
            tokens = word_tokenize(text)
            tagged = pos_tag(tokens)
            tree = ne_chunk(tagged)
            
            for subtree in tree:
                if isinstance(subtree, Tree):
                    entity_name = ' '.join([word for word, _ in subtree.leaves()])
                    entity_type = subtree.label()
                    
                    if entity_type == 'PERSON':
                        entities['people'].add(entity_name)
                    elif entity_type == 'ORGANIZATION':
                        entities['organizations'].add(entity_name)
                    elif entity_type == 'GPE':
                        entities['locations'].add(entity_name)
        except Exception:
            pass
        
        # Keyword-based extraction: match whole words so 'python,' / '(python)'
        # still trigger, and 'java' doesn't match inside 'javascript'. The old
        # set(text.split()) approach relied on whitespace-only tokenization.
        lowered = text.lower()

        # Aliases imply their technology without the bare token appearing:
        # "golang" never contains a standalone "go".
        for canonical, aliases in SUFFICIENT_ALIASES.items():
            if any(re.search(a, lowered) for a in aliases):
                entities['technologies'].add(canonical)
        for kw in self.tech_keywords:
            if kw in AMBIGUOUS_TECH or kw in CANONICAL_TECH:
                emitted = _emit_tech(kw, lowered)
                if emitted:
                    entities['technologies'].add(emitted)
            elif re.search(rf"\b{re.escape(kw)}\b", lowered):
                entities['technologies'].add(kw)
        for kw in self.project_keywords:
            if re.search(rf"\b{re.escape(kw)}\b", lowered):
                entities['projects'].add(kw)
        
        # Convert sets to sorted lists
        return {k: sorted(list(v)) for k, v in entities.items()}


def extract_entities(text: str) -> Dict[str, List[str]]:
    """Convenience function to extract entities."""
    extractor = EntityExtractor()
    return extractor.extract(text)


def extract_people(text: str) -> List[str]:
    """Extract only people entities."""
    entities = extract_entities(text)
    return entities.get('people', [])


def extract_technologies(text: str) -> List[str]:
    """Extract only technology entities."""
    entities = extract_entities(text)
    return entities.get('technologies', [])
