# -*- coding: utf-8 -*-
"""Test-only stub. The action plugin of the same name does the work."""

DOCUMENTATION = r"""
module: cli_command
short_description: Mock cli_command for discovery tests
description: Returns canned CLI output. See tests/mock_cli/README.md.
options:
  command:
    description: CLI command.
    type: str
    required: true
author: vxlan-migration tests
"""
