# `npu-top` Documentation

[中文](./README.md) · **English** · [Back to Project Homepage](../README.en.md)

Welcome to the `npu-top` documentation hub. Here you will find guides on the collection architecture, the read-only CLI and MCP interfaces for AI agents, and Ascend NPU output parsing.

## Guides & References

| Topic | 中文 | English | What You'll Learn |
| --- | --- | --- | --- |
| System Architecture & Polling Tiers | [architecture.zh.md](./architecture.zh.md) | [architecture.md](./architecture.md) | How agentless SSH collection works, how busy/idle NPU cards are classified, and why `npu-top` is strictly observation-only |
| Agent CLI & MCP Tools Guide | [agent-access.md](./agent-access.md) | [agent-access.en.md](./agent-access.en.md) | How AI agents use the 5 read-only MCP tools and CLI commands to screen idle servers, inspect processes, and check disk mounts |
| Ascend 950 (A5) Probing & Parsing | [a5-probing.zh.md](./a5-probing.zh.md) | [a5-probing.md](./a5-probing.md) | How `npu-top` parses the single-ID four-column `npu-smi info` layout on Ascend 950 (A5) servers |
