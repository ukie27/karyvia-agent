# Karyvia Agent

Karyvia Agent is a small, stable Agent Kernel whose optional capabilities are
supplied by plugins.

## Current Status

The kernel is complete and usable: contracts, turn engine, configuration,
observability, routing, plugin runtime, eight built-in capability packages, and the `karyvia`
CLI. Capability plugins are delivered too — seven official plugins ship in
[`plugins/`](./plugins/README.md), covering an extra model provider, three
channels, web/MCP tools, long-term memory, and cron automation.

- The Python package, distribution, and CLI command are all named `karyvia`.
- Named instance data lives in `~/.karyvia/instances/<instance>/`; configuration is
  snake_case and validated against a generated JSON Schema.
- Karyvia is not currently published to PyPI; install it from a local checkout.

## Architecture

Six layers, dependencies flow downward only (rules `R1`–`R5`, enforced by
`tests/architecture/`):

```text
src/karyvia/
├── contracts/   # public data contracts, pure types, zero internal deps
├── kernel/      # mechanism, depends only on contracts
├── sdk/         # the only surface plugins depend on
├── builtins/    # default capabilities, same standing as plugins
├── runtime/     # assembly root + the `karyvia` executable
└── embed/       # embedded Python SDK
```

Capabilities are registered through one `KaryviaAPI` implementation; built-ins and
external plugins share the same load path and lifecycle. Enabled plugins are fully trusted
in-process Python code; the resource facades are services, not a permission sandbox.
Official plugins live in [`plugins/`](./plugins/README.md); runnable minimal
examples are in [`examples/plugins/`](./examples/plugins/README.md).

## Development Setup

Python 3.11 or newer. Work from a local checkout with a virtual environment.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

# Plugins are discovered through entry points, so they must actually be
# installed. `--no-deps` keeps platform SDKs out of the test environment on
# purpose: no plugin's test tree may depend on its vendor SDK.
karyvia plugins install --no-deps examples/plugins/karyvia-plugin-echo-tool
karyvia plugins install --no-deps examples/plugins/karyvia-plugin-session-memory
karyvia plugins install --no-deps plugins/karyvia-plugin-openai-api
karyvia plugins install --no-deps plugins/karyvia-plugin-anthropic
karyvia plugins install --no-deps plugins/karyvia-plugin-feishu
karyvia plugins install --no-deps plugins/karyvia-plugin-web
karyvia plugins install --no-deps plugins/karyvia-plugin-mcp
karyvia plugins install --no-deps plugins/karyvia-plugin-memory
karyvia plugins install --no-deps plugins/karyvia-plugin-cron
```

On Windows use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.

## Common Commands

```bash
# Tests
pytest
pytest tests/architecture -q          # layer guards, run as a separate CI job

# Lint. Do not run ruff format.
ruff check src/ plugins/ examples/ scripts/ tests/

# Strict type checking
uv sync --all-extras --dev
uv run --no-sync basedpyright

# First run, then a turn
karyvia init
karyvia run

# Headless: serve every enabled Channel plugin
karyvia serve

# Diagnostics
karyvia capabilities        # which capabilities actually took effect, and from where
karyvia plugins list
```

## Documentation

Start here:

- [Getting started](./docs/getting-started.md) — install, `karyvia init`, first turn
- [Configuration reference](./docs/configuration.md) — every field, every layer
- [CLI reference](./docs/cli.md) — all eight subcommands and their exit codes
- [Deployment](./docs/deployment.md) — Docker, compose, systemd

Also:

- [Documentation index](./docs/README.md)
- [Current project status](./docs/project/README.md)
- [Writing a plugin](./docs/plugin-development.md)
- [Architecture map](./docs/project/architecture-map.md)
- [Evolution boundaries](./docs/project/evolution-boundaries.md)
- [Common change guide](./docs/project/change-guide.md)
- [Repository instructions](./AGENTS.md)
- [Architecture constraints](./.agent/design.md)
- [Security boundaries](./.agent/security.md)
- [Common implementation gotchas](./.agent/gotchas.md)

## License

Karyvia is distributed under the [MIT license](./LICENSE).
