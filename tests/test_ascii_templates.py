#!/usr/bin/env python3
"""Guard generated Ansible artifacts against unicode dashes (mojibake)."""

import os
import unittest

_REPO = os.path.join(os.path.dirname(__file__), "..")
_UNICODE_DASHES = ("\u2012", "\u2013", "\u2014", "\u2015")
_SCAN_ROOTS = (
    os.path.join(_REPO, "roles"),
    os.path.join(_REPO, "playbooks"),
    os.path.join(_REPO, "plugins"),
)
_SCAN_SUFFIXES = (".j2", ".yml", ".yaml", ".py")


class AsciiDashTests(unittest.TestCase):
    def test_no_unicode_dashes_in_ansible_outputs(self):
        offenders = []
        for root in _SCAN_ROOTS:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    if not name.endswith(_SCAN_SUFFIXES):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8") as handle:
                        text = handle.read()
                    for lineno, line in enumerate(text.splitlines(), 1):
                        if any(dash in line for dash in _UNICODE_DASHES):
                            rel = os.path.relpath(path, _REPO)
                            offenders.append("%s:%s" % (rel, lineno))
        self.assertEqual(
            offenders,
            [],
            "Unicode en/em dashes break on some terminals. Use ASCII '-' instead:\n"
            + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
