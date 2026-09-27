"""Canonicalizing candidates must not remove the system-root denylist."""
import os

import pytest

from botainer.hpc import module_binds as mb


@pytest.fixture
def mac_aliases(monkeypatch):
    def canonical(path):
        for prefix in ("/etc", "/var"):
            if path == prefix or path.startswith(prefix + "/"):
                return "/private" + path
        # A deterministic synthetic host: do not follow the real host's Docker
        # socket symlink while simulating a different /var alias layout.
        return os.path.normpath(path)
    monkeypatch.setattr(mb, "_realpath", canonical)


@pytest.mark.parametrize("path", ["/etc", "/etc/ssl", "/private/etc/ssl",
                                  "/var/run/docker.sock", "/private/var/run/docker.sock"])
def test_module_tree_sensitive_alias_is_refused(mac_aliases, path):
    refused, reason = mb.is_unsafe_module_tree_source(path)
    assert refused and "refused subtree" in reason


@pytest.mark.parametrize("path", ["/private/etc/ssl", "/private/var/lib/sensitive"])
def test_derived_software_root_uses_canonical_deny_roots(mac_aliases, path):
    assert mb._is_system_root(path)


def test_deep_module_tree_and_lookalike_remain_distinct(mac_aliases):
    assert mb.is_unsafe_module_tree_source("/usr/share/lmod/lmod") == (False, "")
    assert mb.is_unsafe_module_tree_source("/private/etc-lookalike/modules") == (False, "")
