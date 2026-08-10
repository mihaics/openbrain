"""Tests for ambiguous-technology guards and the removal of the `auto` tag."""
import pytest


def _tech(text):
    from src.extractors.entities import extract_entities
    return set(extract_entities(text).get("technologies", []))


class TestSufficientAliases:
    """An unambiguous alias implies the technology on its own."""

    @pytest.mark.parametrize("text,expected", [
        ("we rewrote it in golang last week", "go"),
        ("the goroutine leaked", "go"),
        ("see go.mod for the version", "go"),
        ("run gofmt before committing", "go"),
        ("upgraded to go1.24 yesterday", "go"),
        ("the nodejs service crashed", "nodejs"),
        ("check node_modules size", "nodejs"),
        ("npm install failed", "nodejs"),
        ("a restful endpoint", "rest"),
        ("tuned the jvm heap", "java"),
        ("mvn clean with maven", "java"),
    ])
    def test_alias_implies_technology(self, text, expected):
        assert expected in _tech(text)

    def test_go_version_string_needs_no_bare_token(self):
        r"""\bgo\b does not match 'go1.24' at all - '1' is a word character -
        so requiring a bare token alongside it made the rule unreachable."""
        assert "go" in _tech("bumped to go1.25 in CI")


class TestQualifiedContext:
    """An ambiguous bare token needs corroboration."""

    @pytest.mark.parametrize("text", [
        "I need to go test it tomorrow",
        "go build a prototype first",
        "let's go through the list",
        "openbrain is the single source of truth for durable memory",
    ])
    def test_english_verb_go_is_not_the_language(self, text):
        assert "go" not in _tech(text), (
            "the English verb 'go' must not tag a memory as the Go language"
        )

    def test_go_with_context_is_tagged(self):
        assert "go" in _tech("run go mod tidy in the service")

    @pytest.mark.parametrize("text", [
        "the rest of the team agreed",
        "we can rest on that assumption",
    ])
    def test_english_noun_rest_is_not_the_protocol(self, text):
        assert "rest" not in _tech(text)

    def test_rest_with_context_is_tagged(self):
        assert "rest" in _tech("expose a rest api for the client")

    @pytest.mark.parametrize("text", ["frontend work", "the ui was slow"])
    def test_bare_ui_needs_context(self, text):
        """'frontend' alone must not emit `ui` for text never containing it."""
        assert "ui" not in _tech(text)

    def test_ui_with_context_is_tagged(self):
        assert "ui" in _tech("the ui component re-renders twice")

    def test_bare_api_needs_context(self):
        assert "api" not in _tech("that is not the api I meant")

    def test_api_with_context_is_tagged(self):
        assert "api" in _tech("rotate the api key")


class TestCanonicalSpelling:
    def test_node_emits_nodejs_only(self):
        """One technology must not enter under two spellings."""
        tech = _tech("the nodejs upgrade broke npm")
        assert "nodejs" in tech
        assert "node" not in tech


class TestUnambiguousKeywordsUnchanged:
    @pytest.mark.parametrize("text,expected", [
        ("the kubernetes cluster", "kubernetes"),
        ("a postgresql query", "postgresql"),
        ("written in python", "python"),
        ("built with rust", "rust"),
    ])
    def test_still_tagged(self, text, expected):
        assert expected in _tech(text)


class TestAutoTagIsGone:
    def test_settings_default_tags_is_empty(self):
        from src.extractors.tagger import TagConfig

        TagConfig._instance = None
        assert TagConfig.get_instance().default_tags == []

    def test_tagconfig_does_not_fall_back_to_auto(self):
        """Emptying settings.yaml is not enough while the code defaults to
        ['auto'] whenever the key is absent."""
        from src.extractors.tagger import TagConfig

        cfg = TagConfig.__new__(TagConfig)
        cfg._apply({"tags": {}})
        assert cfg.default_tags == []

    def test_generated_config_has_no_auto_tag(self):
        """src/setup writes the config for a fresh install; leaving 'auto'
        there would reintroduce it on any new machine."""
        import inspect

        from src import setup

        assert "'default_tags': ['auto']" not in inspect.getsource(setup)

    def test_tagging_emits_no_auto(self):
        from src.extractors.tagger import auto_tag

        tags = auto_tag("a note about kubernetes", {}, "cli", [])
        assert "auto" not in tags
