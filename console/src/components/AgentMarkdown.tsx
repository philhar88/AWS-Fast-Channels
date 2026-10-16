import {
  colorBackgroundCellShaded,
  colorBorderDividerDefault,
  fontFamilyMonospace,
} from "@cloudscape-design/design-tokens";
import type { CSSProperties } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import styles from "./AgentMarkdown.module.css";

/** Design tokens as custom properties, set through the CSSOM so the strict CSP needs no 'unsafe-inline'. */
const THEME = {
  "--md-border": colorBorderDividerDefault,
  "--md-shaded": colorBackgroundCellShaded,
  "--md-monospace": fontFamilyMonospace,
} as CSSProperties;

/** GitHub-flavoured Markdown: the agent answers with tables (system prompt), and also uses strikethrough and task lists. */
const PLUGINS = [remarkGfm];

const COMPONENTS: Components = {
  // A wide table scrolls inside the chat bubble instead of stretching it.
  table: ({ children }) => (
    <div className={styles.tableScroll}>
      <table>{children}</table>
    </div>
  ),
  // Agent text is untrusted (titles and synopses come from the catalog): links open apart from the console.
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
};

/** An agent reply rendered as Markdown. Raw HTML is never rendered (react-markdown's default). */
export function AgentMarkdown({ text }: { text: string }) {
  return (
    <div className={styles.markdown} style={THEME}>
      <Markdown remarkPlugins={PLUGINS} components={COMPONENTS}>
        {text}
      </Markdown>
    </div>
  );
}
