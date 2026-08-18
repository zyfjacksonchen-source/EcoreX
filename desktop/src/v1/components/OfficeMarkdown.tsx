import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const MARKDOWN_PARSE_LIMIT = 256 * 1024;
const SAFE_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

interface OfficeMarkdownProps {
  text: string;
  streaming: boolean;
}

function isPrivateArtifactUrl(value: string): boolean {
  try {
    return new URL(value, "http://emate.local").pathname.startsWith("/api/v1/artifacts/");
  } catch {
    return false;
  }
}

function safeLinkUrl(value: string): string {
  const url = value.trim();
  if (!url) return "";
  if (isPrivateArtifactUrl(url)) return "";
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

export default function OfficeMarkdown({ text, streaming }: OfficeMarkdownProps) {
  if (text.length > MARKDOWN_PARSE_LIMIT) {
    return (
      <div className="ex-rich-text is-long" data-render-mode="bounded-plain-text">
        <span className="ex-rich-text-notice">
          内容较长，已切换为纯文本显示，避免页面卡顿。
        </span>
        <pre>{text}</pre>
        {streaming ? <span className="ex-stream-caret" aria-hidden="true" /> : null}
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
        {text}
      </ReactMarkdown>
      {streaming ? <span className="ex-stream-caret" aria-hidden="true" /> : null}
    </div>
  );
}
