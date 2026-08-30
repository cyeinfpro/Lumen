import type { Nodes, Root } from "mdast";
import remarkParse from "remark-parse";
import { unified } from "unified";
import { visit } from "unist-util-visit";

const RESERVED_MARKER = /<\/?tool_call>|<function=(?:lumen_create_image|exec_command|apply_patch|bash|read|write|edit|grep|find|ls|store_put|store_get)>/giu;
const RESERVED_TRANSCRIPT_LINE = /^(?:assistant|tool|recipient)\s+(?:to|name|channel)\s*=/giu;

interface Replacement {
  start: number;
  end: number;
  value: string;
}

function neutralize(value: string): string {
  return value
    .replace(RESERVED_MARKER, (marker) =>
      marker.replace("<", "\u2039").replace(">", "\u203a"),
    )
    .replace(RESERVED_TRANSCRIPT_LINE, (marker) => `${marker[0]}\u2060${marker.slice(1)}`);
}

function replaceableNode(node: Nodes): node is Nodes & {
  value: string;
  position: NonNullable<Nodes["position"]>;
} {
  return (
    (node.type === "text" || node.type === "html") &&
    typeof (node as { value?: unknown }).value === "string" &&
    node.position?.start.offset !== undefined &&
    node.position.end.offset !== undefined
  );
}

export function neutralizeAgentPseudoProtocol(markdown: string): string {
  if (!markdown.includes("<") && !/^(?:assistant|tool|recipient)\s/imu.test(markdown)) {
    return markdown;
  }
  const tree = unified().use(remarkParse).parse(markdown) as Root;
  const replacements: Replacement[] = [];
  visit(tree, (node) => {
    if (!replaceableNode(node)) return;
    const value = neutralize(node.value);
    if (value === node.value) return;
    replacements.push({
      start: node.position.start.offset as number,
      end: node.position.end.offset as number,
      value,
    });
  });
  let projected = markdown;
  for (const replacement of replacements.sort((left, right) => right.start - left.start)) {
    projected =
      projected.slice(0, replacement.start) +
      replacement.value +
      projected.slice(replacement.end);
  }
  return projected;
}
