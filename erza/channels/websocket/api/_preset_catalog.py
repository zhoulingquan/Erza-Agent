"""MCP preset catalog: field/meta dataclasses and the builtin preset list."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from erza.config.schema import MCPServerConfig


@dataclass(frozen=True)
class McpPresetField:
    name: str
    label: str
    target: tuple[Literal["env", "url_param", "arg", "header"], str]
    secret: bool = True
    required: bool = True
    env_var: str | None = None
    placeholder: str = ""


@dataclass(frozen=True)
class McpPreset:
    name: str
    display_name: str
    category: str
    description: str
    docs_url: str
    transport: Literal["stdio", "streamableHttp", "sse", "oauth"]
    install_supported: bool
    brand_domain: str
    brand_color: str
    server: MCPServerConfig | None = None
    fields: tuple[McpPresetField, ...] = ()
    requires: str = ""
    note: str = ""


def _favicon_url(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=64"


MCP_PRESETS: tuple[McpPreset, ...] = (
    McpPreset(
        name="playwright",
        display_name="Playwright",
        category="browser",
        description="浏览器自动化 MCP 服务，支持页面操作、截图、表单填写等。",
        docs_url="https://github.com/anthropics/playwright-mcp",
        transport="stdio",
        install_supported=True,
        brand_domain="playwright.dev",
        brand_color="#2EAD33",
        server=MCPServerConfig(
            type="stdio",
            command="npx",
            args=["-y", "@playwright/mcp@latest"],
        ),
        requires="Node.js",
        note="无需 API Key，开箱即用。",
    ),
    McpPreset(
        name="context7",
        display_name="Context7",
        category="docs",
        description="获取最新版库文档，为 AI 提供准确的 API 参考。",
        docs_url="https://github.com/upstash/context7",
        transport="stdio",
        install_supported=True,
        brand_domain="context7.com",
        brand_color="#DD3105",
        server=MCPServerConfig(
            type="stdio",
            command="npx",
            args=["-y", "@upstash/context7-mcp@latest"],
        ),
        # context7 支持可选的 api_key:传入时以 --api-key 参数追加到 args 末尾。
        fields=(
            McpPresetField(
                name="context7_api_key",
                label="Context7 API key",
                target=("arg", "--api-key"),
                secret=True,
                required=False,
                placeholder="ctx7_...",
            ),
        ),
        requires="Node.js",
        note="无需 API Key 即可使用，配置 API Key 可获得更高的速率配额。",
    ),
    McpPreset(
        name="sequential-thinking",
        display_name="Sequential Thinking",
        category="reasoning",
        description="结构化思维工具，帮助 AI 分步骤解决复杂问题。",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking",
        transport="stdio",
        install_supported=True,
        brand_domain="modelcontextprotocol.io",
        brand_color="#6366F1",
        server=MCPServerConfig(
            type="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-sequential-thinking"],
        ),
        requires="Node.js",
        note="无需 API Key，开箱即用。",
    ),
    McpPreset(
        name="fetch",
        display_name="Fetch",
        category="web",
        description="网页抓取工具，获取 URL 内容并转为 Markdown。",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
        transport="stdio",
        install_supported=True,
        brand_domain="modelcontextprotocol.io",
        brand_color="#0EA5E9",
        server=MCPServerConfig(
            type="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-fetch"],
        ),
        requires="Node.js",
        note="无需 API Key，开箱即用。",
    ),
    McpPreset(
        name="filesystem",
        display_name="Filesystem",
        category="files",
        description="文件系统访问工具，允许 AI 读写指定目录的文件。",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
        transport="stdio",
        install_supported=True,
        brand_domain="modelcontextprotocol.io",
        brand_color="#10B981",
        server=MCPServerConfig(
            type="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/"],
        ),
        requires="Node.js",
        note="默认允许访问根目录，可根据需要修改参数中的路径。",
    ),
    McpPreset(
        name="github",
        display_name="GitHub",
        category="dev",
        description="GitHub 仓库管理，支持搜索、创建 Issue、管理 PR 等。",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/github",
        transport="stdio",
        install_supported=True,
        brand_domain="github.com",
        brand_color="#181717",
        server=MCPServerConfig(
            type="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
        ),
        fields=(
            McpPresetField(
                name="api_key",
                label="GitHub Personal Access Token",
                target=("env", "GITHUB_PERSONAL_ACCESS_TOKEN"),
                secret=True,
                required=True,
                env_var="GITHUB_PERSONAL_ACCESS_TOKEN",
                placeholder="ghp_xxxxxxxxxxxx",
            ),
        ),
        requires="Node.js + GitHub Token",
        note="在 GitHub Settings → Developer settings → Personal access tokens 生成 Token。",
    ),
    McpPreset(
        name="memory",
        display_name="Memory",
        category="knowledge",
        description="持久化记忆工具，基于知识图谱存储和检索信息。",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
        transport="stdio",
        install_supported=True,
        brand_domain="modelcontextprotocol.io",
        brand_color="#8B5CF6",
        server=MCPServerConfig(
            type="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-memory"],
        ),
        requires="Node.js",
        note="无需 API Key，开箱即用。",
    ),
    McpPreset(
        name="puppeteer",
        display_name="Puppeteer",
        category="browser",
        description="Puppeteer 浏览器自动化，支持页面截图、PDF 生成、表单提交等。",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/puppeteer",
        transport="stdio",
        install_supported=True,
        brand_domain="pptr.dev",
        brand_color="#40B5A4",
        server=MCPServerConfig(
            type="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-puppeteer"],
        ),
        requires="Node.js",
        note="无需 API Key，开箱即用。首次运行会下载 Chromium。",
    ),
    McpPreset(
        name="time",
        display_name="Time",
        category="utility",
        description="时间工具，获取当前时间、时区转换等。",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/time",
        transport="stdio",
        install_supported=True,
        brand_domain="modelcontextprotocol.io",
        brand_color="#F59E0B",
        server=MCPServerConfig(
            type="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-time"],
        ),
        requires="Node.js",
        note="无需 API Key，开箱即用。",
    ),
)
