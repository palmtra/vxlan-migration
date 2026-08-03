# Documentation agent instructions

Guidance for AI agents (and humans) maintaining documentation in this repository.

## Documentation layout

```
docs/
├── README.md              # Master index — always update when adding docs
├── AGENTS.md              # This file
├── WORKFLOWS.md           # Architecture (Core vs Advanced, mermaid)
├── VXLAN_SERVICE_TYPES.md # L2 vs L3 vs l2_l3 service_type guide
├── USAGE_GUIDE.md         # Full operator reference (flags, variables, AAP)
├── ROADMAP.md             # Planned and completed work
├── OPERATING_GUIDE.md     # Redirect only — do not duplicate content here
├── WORKFLOW_V2.md         # Historical — do not delete; mark superseded sections in WORKFLOWS.md instead
└── examples/
    ├── README.md          # Examples index
    └── *.md               # One task per file (short, copy-paste commands)
```

## Where to put new content

| Content type | Location | Max scope |
|---|---|---|
| Architecture, workflow tiers, SSOT model | `docs/WORKFLOWS.md` | Conceptual; link to examples for commands |
| L2 vs L3 migration intent, `service_type` | `docs/VXLAN_SERVICE_TYPES.md` | Conceptual; link from VLAN DB schema |
| Variable / flag reference, troubleshooting | `docs/USAGE_GUIDE.md` | Comprehensive tables |
| Runnable command for one task | `docs/examples/<task>.md` | One page, one goal |
| Planned features | `docs/ROADMAP.md` | Bullet list with checkboxes |

**Do not** add long command blocks to `WORKFLOWS.md` or root `README.md` — link to an example file instead.

## Adding a new example

1. Create `docs/examples/<kebab-case-task>.md`
2. Include: purpose, prerequisites, commands, expected output paths, related variables
3. Add a row to `docs/examples/README.md`
4. Add a row to `docs/README.md` under **Examples**
5. Optionally add a one-line link from `USAGE_GUIDE.md` (§ relevant section)

## Updating the index

Whenever you add, rename, or remove a doc under `docs/`:

1. Update `docs/README.md` tables
2. Update `docs/examples/README.md` if under `examples/`
3. Update root `README.md` **Documentation** section if the doc is user-facing

## Writing style

- Complete sentences; avoid telegraphic bullet chains in prose
- Use fenced code blocks with full commands (no `...` omissions in examples)
- Prefer linking between docs over duplicating content
- Mark historical docs as superseded rather than deleting them without user request
- Do not create new top-level markdown files in the repo root for docs — use `docs/`

## Commit conventions

- `docs:` prefix for documentation-only commits
- `feat:` / `fix:` for code + doc updates in the same change when behaviour changed
- Conventional commits per GitLab workflow rules

## Code ↔ doc sync checks

When changing behaviour, verify these stay aligned:

| Code area | Documentation |
|---|---|
| `cvp_apply_configlets` default / gating | `examples/generate-config-without-cvp-push.md`, USAGE_GUIDE § Core deploy |
| Core vs Advanced playbooks | WORKFLOWS.md mermaid diagrams |
| Discovery report fields | USAGE_GUIDE § Discovery, examples/discover-vlan.md |
| `vars/vlans/` schema | WORKFLOWS.md VLAN DB table, [VXLAN_SERVICE_TYPES.md](VXLAN_SERVICE_TYPES.md), USAGE_GUIDE, root README data model |
| NetBox export playbook | examples/export-vlan-to-netbox.md |

## What not to document here

- Greenfield / new VLAN deployment app (out of scope for this migration repo)
- Automatic trunk cleanup commands (discovery is report-only; cleanup is `decomm-vlan`)
