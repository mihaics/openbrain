"""Tests for the project identity attached to captured memories.

Subtree matching means the session cwd is no longer the project root: a
session started in `repo/src` and one started in `repo/tests` belong to the
same project. Identity is therefore the nearest enclosing repository, not the
working directory.
"""
import pytest


def _identity(cwd, root):
    from src.capture.identity import project_for
    return project_for(str(cwd), str(root))


@pytest.fixture
def tree(tmp_path):
    """A work root holding a repository, mirroring ~/work/<group>/<repo>."""
    root = tmp_path / "work"
    repo = root / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    return root, repo


class TestNearestRepository:
    def test_cwd_at_the_repository_root(self, tree):
        root, repo = tree
        assert _identity(repo, root) == str(repo)

    def test_cwd_in_a_subdirectory_resolves_to_the_repository(self, tree):
        root, repo = tree
        assert _identity(repo / "src", root) == str(repo)

    def test_deeply_nested_cwd_resolves_to_the_repository(self, tree):
        root, repo = tree
        deep = repo / "src" / "a" / "b" / "c"
        deep.mkdir(parents=True)

        assert _identity(deep, root) == str(repo)

    def test_repository_sitting_exactly_at_the_boundary(self, tmp_path):
        """The allowlist root may itself be a repository, as open-brain is."""
        root = tmp_path / "open-brain"
        (root / ".git").mkdir(parents=True)
        (root / "src").mkdir()

        assert _identity(root / "src", root) == str(root)

    def test_no_marker_falls_back_to_the_resolved_cwd(self, tmp_path):
        root = tmp_path / "work"
        plain = root / "scratch" / "notes"
        plain.mkdir(parents=True)

        assert _identity(plain, root) == str(plain)


class TestFileFormMarkers:
    """Worktrees and submodules use a `.git` file, not a directory."""

    def test_linked_worktree_with_an_absolute_gitdir(self, tree):
        root, repo = tree
        tree_dir = repo / ".worktrees" / "feature"
        tree_dir.mkdir(parents=True)
        (tree_dir / ".git").write_text(
            f"gitdir: {repo}/.git/worktrees/feature\n")
        (repo / ".git" / "worktrees" / "feature").mkdir(parents=True)
        (tree_dir / "src").mkdir()

        assert _identity(tree_dir / "src", root) == str(tree_dir)

    def test_submodule_with_a_relative_gitdir(self, tree):
        """The real submodules use `gitdir: ../.git/modules/<name>`."""
        root, repo = tree
        sub = repo / "vendor"
        sub.mkdir()
        (sub / ".git").write_text("gitdir: ../.git/modules/vendor\n")
        (repo / ".git" / "modules" / "vendor").mkdir(parents=True)

        assert _identity(sub, root) == str(sub)

    def test_nearest_marker_wins_for_a_worktree_inside_a_worktree(self, tree):
        root, repo = tree
        outer = repo / ".worktrees" / "outer"
        inner = outer / ".worktrees" / "inner"
        inner.mkdir(parents=True)
        for path in (outer, inner):
            (path / ".git").write_text(f"gitdir: {repo}/.git/worktrees/x\n")
        (repo / ".git" / "worktrees" / "x").mkdir(parents=True)

        assert _identity(inner, root) == str(inner)

    def test_nested_repository_wins_over_its_container(self, tree):
        root, repo = tree
        nested = repo / "themes" / "vendored"
        (nested / ".git").mkdir(parents=True)

        assert _identity(nested / "layouts", root) == str(nested)


class TestMalformedMarkers:
    """A `.git` file that is not a real pointer must not end the walk."""

    @pytest.mark.parametrize("body", ["", "   \n", "not a pointer\n",
                                      "gitdir:\n", "gitdir:   \n"])
    def test_malformed_git_file_is_not_a_marker(self, tree, body):
        root, repo = tree
        fake = repo / "src" / "fake"
        fake.mkdir()
        (fake / ".git").write_text(body)

        assert _identity(fake, root) == str(repo)

    def test_dangling_gitdir_target_is_not_a_marker(self, tree):
        root, repo = tree
        fake = repo / "src" / "fake"
        fake.mkdir()
        (fake / ".git").write_text("gitdir: /nonexistent/elsewhere\n")

        assert _identity(fake, root) == str(repo)

    def test_unreadable_git_file_is_not_a_marker(self, tree):
        root, repo = tree
        fake = repo / "src" / "fake"
        fake.mkdir()
        marker = fake / ".git"
        marker.write_text("gitdir: /tmp\n")
        marker.chmod(0o000)
        try:
            assert _identity(fake, root) == str(repo)
        finally:
            marker.chmod(0o600)


class TestBoundary:
    def test_never_looks_above_the_allowlist_root(self, tmp_path):
        """A repository containing the whole work tree must not claim it."""
        outer = tmp_path / "outer"
        (outer / ".git").mkdir(parents=True)
        root = outer / "work"
        plain = root / "scratch"
        plain.mkdir(parents=True)

        assert _identity(plain, root) == str(plain)

    def test_marker_at_the_boundary_itself_is_used(self, tmp_path):
        root = tmp_path / "work"
        (root / ".git").mkdir(parents=True)
        deep = root / "a" / "b"
        deep.mkdir(parents=True)

        assert _identity(deep, root) == str(root)

    def test_cwd_outside_the_root_falls_back_to_the_cwd(self, tmp_path):
        """Defensive: the caller should never do this, so do not walk wild."""
        root = tmp_path / "work"
        root.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()

        assert _identity(outside, root) == str(outside)
