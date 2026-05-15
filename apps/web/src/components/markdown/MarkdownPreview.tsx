"use client";

type Props = {
  bodyHtml: string;
  className?: string;
  limitLines?: number;
};

export function MarkdownPreview({
  bodyHtml,
  className,
  limitLines,
}: Props) {
  return (
    <div
      className={className}
      style={limitLines ? { maxHeight: `${limitLines * 1.75}em` } : undefined}
      dangerouslySetInnerHTML={{ __html: bodyHtml }}
    />
  );
}
