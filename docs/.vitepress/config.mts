import { defineConfig } from "vitepress";
import { withMermaid } from "vitepress-plugin-mermaid";

function normalizeBase(value: string | undefined): string {
  if (!value || value === "/") return "/";
  return `/${value.replace(/^\/+|\/+$/g, "")}/`;
}

export default withMermaid(
  defineConfig({
    lang: "zh-CN",
    title: "Handwritten Agent Core",
    description: "不依赖 LangChain/LangGraph 的可复用手写 Agent 框架核心",
    base: normalizeBase(process.env.DOCS_BASE),
    cleanUrls: false,
    lastUpdated: true,
    markdown: {
      lineNumbers: true,
    },
    head: [
      ["meta", { name: "theme-color", content: "#167d72" }],
      ["meta", { name: "robots", content: "index,follow" }],
    ],
    themeConfig: {
      logo: "/logo.svg",
      siteTitle: "Agent Core",
      nav: [
        { text: "指南", link: "/guide/getting-started" },
        { text: "架构", link: "/ARCHITECTURE" },
        { text: "API", link: "/reference/public-api" },
        {
          text: "项目",
          items: [
            {
              text: "源码",
              link: "https://github.com/qiuzixu/agent-core",
            },
            {
              text: "问题反馈",
              link: "https://github.com/qiuzixu/agent-core/issues",
            },
          ],
        },
      ],
      sidebar: [
        {
          text: "开始使用",
          items: [
            { text: "文档首页", link: "/" },
            { text: "快速开始", link: "/guide/getting-started" },
            { text: "核心概念", link: "/guide/core-concepts" },
            { text: "已实现能力", link: "/CAPABILITIES" },
          ],
        },
        {
          text: "核心能力",
          items: [
            { text: "Agent Loop", link: "/guide/agent-loop" },
            { text: "模型与适配器", link: "/guide/models" },
            { text: "工具与 MCP", link: "/guide/tools-and-mcp" },
            { text: "Skill", link: "/guide/skills" },
            { text: "中间件与 HITL", link: "/guide/middleware-and-hitl" },
            { text: "上下文与记忆", link: "/guide/context-and-memory" },
            {
              text: "工作流与 Checkpoint",
              link: "/guide/workflows-and-checkpoints",
            },
            { text: "存储与生产部署", link: "/guide/storage-and-production" },
            { text: "ACP", link: "/guide/acp" },
            {
              text: "可观测性与 Guardrails",
              link: "/guide/observability-and-guardrails",
            },
          ],
        },
        {
          text: "参考",
          items: [
            { text: "系统架构", link: "/ARCHITECTURE" },
            { text: "公共 API", link: "/reference/public-api" },
            { text: "配置参考", link: "/reference/configuration" },
            { text: "异常体系", link: "/reference/exceptions" },
          ],
        },
        {
          text: "维护者",
          items: [
            { text: "发布流程", link: "/maintainers/release" },
            {
              text: "贡献指南",
              link: "https://github.com/qiuzixu/agent-core/blob/main/CONTRIBUTING.md",
            },
          ],
        },
      ],
      search: {
        provider: "local",
      },
      outline: {
        level: [2, 3],
        label: "本页内容",
      },
      docFooter: {
        prev: "上一页",
        next: "下一页",
      },
      lastUpdated: {
        text: "最后更新",
        formatOptions: {
          dateStyle: "medium",
          timeStyle: "short",
        },
      },
      editLink: {
        pattern: "https://github.com/qiuzixu/agent-core/edit/main/docs/:path",
        text: "编辑此页",
      },
      footer: {
        message: "基于 Apache-2.0 许可证发布",
        copyright: "Copyright 2026 Agent Core contributors",
      },
    },
    mermaid: {
      theme: "neutral",
      flowchart: {
        htmlLabels: false,
        curve: "basis",
      },
    },
    vite: {
      optimizeDeps: {
        include: ["fastdom"],
      },
    },
  }),
);
