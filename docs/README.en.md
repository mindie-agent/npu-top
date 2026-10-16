# `npu-top` Documentation

[中文](./README.md) · **English** · [Back to Project Homepage](../README.en.md)

This directory contains the architecture overview, observation-only contract, Agent CLI / MCP interface specifications, and Ascend NPU probing notes for `npu-top`.

## Documents

| Document | 中文 | English | Summary |
| --- | --- | --- | --- |
| System Architecture & Collection | [architecture.zh.md](./architecture.zh.md) | [architecture.md](./architecture.md) | Host SSH access, inventory merging, observation-only contract, collection tiers, and SQLite persistence |
| Agent CLI & MCP Reference | [agent-access.md](./agent-access.md) | [agent-access.en.md](./agent-access.en.md) | `observation` envelope schema, CLI commands, 5 read-only MCP tools, and loopback HTTP endpoints |
| Ascend 950 (A5) Probing & Parsing | [a5-probing.zh.md](./a5-probing.zh.md) | [a5-probing.md](./a5-probing.md) | Single-ID four-column `npu-smi info` parsing and regression fixtures for Ascend 950 (A5) hosts |
