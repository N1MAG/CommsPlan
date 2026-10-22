import { defineConfig } from "vitepress";

export default defineConfig({
  lang: "en-US",
  title: "CommsPlan",
  titleTemplate: ":title · CommsPlan",
  description:
    "CommsPlan makes your comms plan work. Where to be, when to be there, and what to do when you’re there.",
  base: "/CommsPlan/",
  cleanUrls: true,
  lastUpdated: true,
  srcExclude: ["README.md"],
  ignoreDeadLinks: false,
  head: [
    ["link", { rel: "icon", type: "image/png", href: "/CommsPlan/commsplan-mark.png" }],
    ["meta", { name: "theme-color", content: "#12324a" }],
    ["meta", { property: "og:type", content: "website" }],
    ["meta", { property: "og:title", content: "CommsPlan makes your comms plan work." }],
    [
      "meta",
      {
        property: "og:description",
        content: "Where to be. When to be there. What to do when you’re there. Bring plans, radio tools, and message intelligence together.",
      },
    ],
  ],
  markdown: {
    theme: {
      light: "github-light",
      dark: "github-dark",
    },
  },
  themeConfig: {
    logo: "/commsplan-mark.png",
    siteTitle: "CommsPlan",
    nav: [
      { text: "Start here", link: "/start-here" },
      { text: "Install or upgrade", link: "/install/" },
      { text: "Set up a radio", link: "/guide/first-radio" },
      { text: "Operate the station", link: "/guide/operating-picture" },
      { text: "What's new in 2.0.6", link: "/guide/whats-new" },
      { text: "Mesh", link: "/integrations/mesh" },
      { text: "Support", link: "/support" },
    ],
    sidebar: [
      {
        text: "Start here",
        items: [
          { text: "Welcome to CommsPlan", link: "/start-here" },
          { text: "Install or upgrade", link: "/install/" },
        ],
      },
      {
        text: "Set up your station",
        items: [{ text: "Configure your first radio", link: "/guide/first-radio" }],
      },
      {
        text: "Operate your station",
        items: [
          { text: "Operating picture", link: "/guide/operating-picture" },
          { text: "What's new in 2.0.6", link: "/guide/whats-new" },
        ],
      },
      {
        text: "Integrations",
        items: [{ text: "Local Mesh", link: "/integrations/mesh" }],
      },
      {
        text: "Help",
        items: [{ text: "Support and diagnostics", link: "/support" }],
      },
    ],
    search: {
      provider: "local",
      options: {
        detailedView: true,
      },
    },
    socialLinks: [
      { icon: "github", link: "https://github.com/N1MAG/CommsPlan" },
    ],
    editLink: {
      pattern: "https://github.com/N1MAG/CommsPlan/edit/main/website/:path",
      text: "Suggest a correction",
    },
    lastUpdated: {
      text: "Last reviewed",
      formatOptions: {
        dateStyle: "medium",
      },
    },
    outline: {
      level: [2, 3],
      label: "On this page",
    },
    docFooter: {
      prev: "Previous",
      next: "Next",
    },
    footer: {
      message: "Operator-focused documentation for CommsPlan.",
      copyright: "CommsPlan is licensed under GNU GPL v3.",
    },
    returnToTopLabel: "Return to top",
    sidebarMenuLabel: "Documentation menu",
    darkModeSwitchLabel: "Appearance",
  },
});
