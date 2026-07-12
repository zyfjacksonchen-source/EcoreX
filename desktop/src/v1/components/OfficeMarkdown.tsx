import { useDeferredValue, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const MARKDOWN_PARSE_LIMIT = 256 * 1024;
const STREAM_FLUSH_MS = 48;
const SAFE_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

interface OfficeMarkdownProps {
  text: string;
  streaming: boolean;
}

function safeLinkUrl(value: string): string {
  const url = value.trim();
  if (!url) return "";
  if (
    url.startsWith("#")
    || url.startsWith("/")
    || url.startsWith("./")
    || url.startsWith("../")
  ) return url;
  if (url.startsWith("//")) return "";
  try {
    const parsed = new URL(url);
    return SAFE_PROTOCOLS.has(parsed.protocol) ? url : "";
  } catch {
    return "";
  }
}

function useBufferedText(text: string, streaming: boolean): string {
  const latest = useRef(text);
  const timer = useRef<number | null>(null);
  const [visible, setVisible] = useState(text);

  useEffect(() => {
    latest.current = text;
    if (!streaming) {
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = null;
      setVisible(text);
      return;
    }
    if (timer.current === null) {
      timer.current = window.setTimeout(() => {
        timer.current = null;
        setVisible(latest.current);
      }, STREAM_FLUSH_MS);
    }
  }, [streaming, text]);

  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);

  return visible;
}

export default function OfficeMarkdown({ text, streaming }: OfficeMarkdownProps) {
  const buffered = useBufferedText(text, streaming);
  const deferred = useDeferredValue(buffered);

  if (deferred.length > MARKDOWN_PARSE_LIMIT) {
    return (
      <div className="ex-rich-text is-long" data-render-mode="bounded-plain-text">
        <span className="ex-rich-text-notice">
          内容较长，已切换为纯文本显示，避免页面卡顿。
        </span>
        <pre>{deferred}</pre>
      </div>
    );
  }

  return (
    <div className="ex-rich-text" data-render-mode="safe-gfm">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[[remarkGfm, { singleTilde: false }]]}
        urlTransform={safeLinkUrl}
        components={{
          a: ({ children, href }) => href ? (
            <a href={href} rel="noreferrer noopener" target="_blank">{children}</a>
          ) : <span>{children}</span>,
          img: ({ alt }) => (
            <span className="ex-rich-text-image-placeholder">
              {alt ? `图片：${alt}` : "图片链接已隐藏"}
            </span>
          ),
          table: ({ children }) => (
            <div className="ex-rich-text-table" role="region" aria-label="消息中的表格" tabIndex={0}>
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {deferred}
      </ReactMarkdown>
    </div>
  );
}
