import {
  ChevronLeft,
  ChevronRight,
  FilePlus2,
  FileText,
  Folder,
  FolderPlus,
  Network,
  RefreshCw,
  Search,
  Upload,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type {
  KnowledgeDocument,
  KnowledgeGraph,
  KnowledgeImportResponse,
  KnowledgeNode,
  KnowledgeTree,
  MemoryContentDocument,
  MemoryContentPage,
  MemoryContentView,
  MemoryLearningSettings,
} from "../api/contracts.ts";
import type { RuntimeClient } from "../api/runtimeClient.ts";
import { userFacingError } from "../state/userLanguage.ts";


function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(value: string | null): string {
  if (!value) return "未知";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function firstDocument(nodes: readonly KnowledgeNode[]): string | null {
  for (const node of nodes) {
    if (node.kind === "document") return node.path;
    const nested = firstDocument(node.children);
    if (nested) return nested;
  }
  return null;
}

function resolveKnowledgeLink(source: string, rawHref: string): string | null {
  const href = rawHref.split(/[?#]/u, 1)[0]?.trim();
  if (!href || href.startsWith("/") || /^[a-z][a-z0-9+.-]*:/iu.test(href) || href.startsWith("//")) return null;
  const parts = source.split("/").slice(0, -1);
  try {
    for (const part of href.split("/")) {
      if (!part || part === ".") continue;
      if (part === "..") {
        if (!parts.length) return null;
        parts.pop();
      } else parts.push(decodeURIComponent(part));
    }
  } catch {
    return null;
  }
  return parts.join("/");
}

function httpsLink(rawHref: string): string | null {
  try {
    const parsed = new URL(rawHref);
    return parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

function KnowledgeTreeNodes({
  nodes,
  selected,
  onSelectDocument,
  onSelectCategory,
}: {
  nodes: readonly KnowledgeNode[];
  selected: string | null;
  onSelectDocument: (path: string) => void;
  onSelectCategory: (path: string) => void;
}) {
  if (!nodes.length) return null;
  return (
    <ul className="ex-knowledge-tree-list">
      {nodes.map((node) => (
        <li key={node.path}>
          {node.kind === "category" ? (
            <details>
              <summary aria-current={selected === node.path ? "true" : undefined} onClick={() => onSelectCategory(node.path)}><Folder aria-hidden="true" /><span>{node.name}</span></summary>
              <KnowledgeTreeNodes nodes={node.children} selected={selected} onSelectDocument={onSelectDocument} onSelectCategory={onSelectCategory} />
            </details>
          ) : (
            <button className="ex-button" type="button" aria-current={selected === node.path ? "true" : undefined} onClick={() => onSelectDocument(node.path)}><FileText aria-hidden="true" /><span>{node.name}</span></button>
          )}
        </li>
      ))}
    </ul>
  );
}

export function KnowledgeSettings({ active, client }: { active: boolean; client: RuntimeClient }) {
  const [tab, setTab] = useState<"documents" | "graph">("documents");
  const [tree, setTree] = useState<KnowledgeTree | null>(null);
  const [document, setDocument] = useState<KnowledgeDocument | null>(null);
  const [graph, setGraph] = useState<KnowledgeGraph | null>(null);
  const [selectedCategory, setSelectedCategory] = useState("");
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createMode, setCreateMode] = useState<"category" | "document" | null>(null);
  const [createPath, setCreatePath] = useState("");
  const [createContent, setCreateContent] = useState("");
  const [importResult, setImportResult] = useState<KnowledgeImportResponse | null>(null);
  const importInput = useRef<HTMLInputElement>(null);

  const loadDocument = useCallback(async (path: string, signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const result = await client.knowledgeDocument(path, signal);
      if (!signal?.aborted) {
        setDocument(result);
        setSelectedCategory(result.path.split("/").slice(0, -1).join("/"));
        setTab("documents");
      }
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) {
        setError(userFacingError(cause));
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [client]);

  const refreshTree = useCallback(async (search: string, signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const result = await client.knowledgeTree(search, signal);
      if (signal?.aborted) return;
      setTree(result);
      if (!document) {
        const initial = firstDocument(result.items);
        if (initial) void loadDocument(initial, signal);
      }
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) {
        setError(userFacingError(cause));
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [client, document, loadDocument]);

  const refreshGraph = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const result = await client.knowledgeGraph(signal);
      if (!signal?.aborted) setGraph(result);
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) {
        setError(userFacingError(cause));
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [client]);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    if (tab === "graph") void refreshGraph(controller.signal);
    else void refreshTree(query, controller.signal);
    return () => controller.abort();
  }, [active, query, refreshGraph, refreshTree, tab]);

  const beginCreate = (mode: "category" | "document") => {
    setCreateMode(mode);
    setCreatePath(selectedCategory ? `${selectedCategory}/` : "");
    setCreateContent("");
    setError(null);
  };

  const create = async () => {
    if (!createMode || !createPath.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      if (createMode === "category") {
        const created = await client.createKnowledgeCategory(createPath.trim(), crypto.randomUUID());
        setSelectedCategory(created.path);
      } else {
        const created = await client.createKnowledgeDocument(createPath.trim(), createContent, crypto.randomUUID());
        setDocument(created);
        setSelectedCategory(created.path.split("/").slice(0, -1).join("/"));
      }
      setCreateMode(null);
      await refreshTree(query);
    } catch (cause) {
      setError(userFacingError(cause));
    } finally {
      setBusy(false);
    }
  };

  const importFiles = async (files: FileList | null) => {
    if (!files?.length || busy) return;
    const selected = [...files];
    if (
      selected.length > 100
      || selected.some((file) => file.size > 10 * 1024 * 1024)
      || selected.reduce((total, file) => total + file.size, 0) > 200 * 1024 * 1024
    ) {
      setError("一次最多导入 100 个文件；单个不超过 10 MiB，总计不超过 200 MiB。");
      if (importInput.current) importInput.current.value = "";
      return;
    }
    setBusy(true);
    setError(null);
    setImportResult(null);
    try {
      const result = await client.importKnowledgeDocuments(selected, selectedCategory, crypto.randomUUID());
      setImportResult(result);
      await refreshTree(query);
      const first = result.items.find((item) => item.path !== null)?.path;
      if (first) await loadDocument(first);
    } catch (cause) {
      setError(userFacingError(cause));
    } finally {
      setBusy(false);
      if (importInput.current) importInput.current.value = "";
    }
  };

  const linkedPaths = useMemo(() => new Set(document?.links ?? []), [document?.links]);

  return (
    <section className="ex-settings-section ex-content-settings" id="settings-knowledge" hidden={!active}>
      <h2>知识</h2>
      <div className="ex-content-heading">
        <p>管理工作区 knowledge 目录中的真实 Markdown 与文本文件。</p>
        <div className="ex-content-actions">
          <button className="ex-button" type="button" disabled={busy} onClick={() => beginCreate("category")}><FolderPlus aria-hidden="true" />新建分类</button>
          <button className="ex-button" type="button" disabled={busy} onClick={() => beginCreate("document")}><FilePlus2 aria-hidden="true" />新建文档</button>
          <label className="ex-button ex-content-import">
            <Upload aria-hidden="true" />导入
            <input ref={importInput} type="file" multiple accept=".md,.txt,text/markdown,text/plain" disabled={busy} onChange={(event) => void importFiles(event.target.files)} />
          </label>
        </div>
      </div>
      <div className="ex-content-tabs" role="tablist" aria-label="知识视图">
        <button className="ex-button" type="button" role="tab" aria-selected={tab === "documents"} onClick={() => setTab("documents")}><FileText aria-hidden="true" />文档</button>
        <button className="ex-button" type="button" role="tab" aria-selected={tab === "graph"} onClick={() => setTab("graph")}><Network aria-hidden="true" />关系图</button>
        <button className="ex-button ex-content-refresh" type="button" aria-label="刷新知识" disabled={loading} onClick={() => tab === "graph" ? void refreshGraph() : void refreshTree(query)}><RefreshCw aria-hidden="true" /></button>
      </div>
      {createMode ? (
        <form className="ex-content-create" onSubmit={(event) => { event.preventDefault(); void create(); }}>
          <label><span>{createMode === "category" ? "分类路径" : "文档路径"}</span><input autoFocus value={createPath} placeholder={createMode === "category" ? "例如：项目/方案" : "例如：项目/方案.md"} onChange={(event) => setCreatePath(event.target.value)} /></label>
          {createMode === "document" ? <label><span>初始内容</span><textarea value={createContent} placeholder="# 标题" onChange={(event) => setCreateContent(event.target.value)} /></label> : null}
          <div><button className="ex-button" type="button" disabled={busy} onClick={() => setCreateMode(null)}>取消</button><button className="ex-button is-primary" type="submit" disabled={busy || !createPath.trim()}>{busy ? "正在创建" : "创建"}</button></div>
        </form>
      ) : null}
      {error ? <div className="ex-settings-error" role="alert"><span>{error}</span><button className="ex-button" type="button" onClick={() => setError(null)}>关闭</button></div> : null}
      {importResult ? (
        <div className="ex-knowledge-import-result" role="status">
          <strong>已导入 {importResult.imported_count} 个文件{importResult.rejected_count ? `，拒绝 ${importResult.rejected_count} 个` : ""}</strong>
          {importResult.items.some((item) => item.status !== "imported") ? (
            <ul>{importResult.items.filter((item) => item.status !== "imported").map((item, index) => (
              <li key={`${item.original_name}-${index}`}>{item.status === "renamed" ? `${item.original_name} 已重命名为 ${item.path}` : `${item.original_name}：${item.reason}`}</li>
            ))}</ul>
          ) : null}
          <button className="ex-button" type="button" onClick={() => setImportResult(null)}>关闭</button>
        </div>
      ) : null}
      {tab === "documents" ? (
        <div className="ex-content-browser">
          <aside className="ex-knowledge-sidebar" aria-label="知识目录">
            <form className="ex-content-search" role="search" onSubmit={(event) => { event.preventDefault(); setQuery(queryInput.trim()); }}>
              <Search aria-hidden="true" /><input aria-label="搜索知识" value={queryInput} placeholder="搜索标题或正文" onChange={(event) => setQueryInput(event.target.value)} /><button className="ex-button" type="submit">搜索</button>
            </form>
            {selectedCategory ? <p className="ex-content-location">导入位置：knowledge/{selectedCategory}</p> : <p className="ex-content-location">导入位置：knowledge</p>}
            {tree?.items.length ? (
              <KnowledgeTreeNodes
                nodes={tree.items}
                selected={document?.path ?? selectedCategory}
                onSelectDocument={(path) => void loadDocument(path)}
                onSelectCategory={(path) => { setSelectedCategory(path); setDocument(null); }}
              />
            ) : <p className="ex-content-empty">{loading ? "正在读取知识目录…" : query ? "没有匹配的知识内容。" : "知识目录为空，可新建或导入文档。"}</p>}
          </aside>
          <article className="ex-content-preview" aria-label="知识文档">
            {document ? (
              <>
                <header><div><h3>{document.name}</h3><p>{document.path} · {formatBytes(document.size_bytes)} · {formatTime(document.updated_at)}</p></div></header>
                <div className="ex-content-markdown">
                  <ReactMarkdown
                    skipHtml
                    remarkPlugins={[remarkGfm]}
                    components={{
                      img: ({ alt = "" }) => <span>{alt}</span>,
                      a: ({ href = "", children, ...props }) => {
                        const target = resolveKnowledgeLink(document.path, href);
                        if (target && linkedPaths.has(target)) {
                          return <a {...props} href="#" onClick={(event) => { event.preventDefault(); void loadDocument(target); }}>{children}</a>;
                        }
                        const external = httpsLink(href);
                        return external ? <a {...props} href={external} target="_blank" rel="noreferrer">{children}</a> : <span>{children}</span>;
                      },
                    }}
                  >{document.content}</ReactMarkdown>
                </div>
                {document.links.length ? <nav className="ex-knowledge-links" aria-label="内部链接"><strong>内部链接</strong>{document.links.map((path) => <button className="ex-button" type="button" key={path} onClick={() => void loadDocument(path)}>{path}</button>)}</nav> : null}
              </>
            ) : <p className="ex-content-empty">从左侧选择文档查看内容。</p>}
          </article>
        </div>
      ) : (
        <div className="ex-knowledge-graph" aria-busy={loading}>
          {graph?.nodes.length ? (
            <>
              <div className="ex-graph-nodes" aria-label="知识节点">{graph.nodes.map((node) => <button className="ex-button" type="button" key={node.path} onClick={() => void loadDocument(node.path)}><FileText aria-hidden="true" /><span>{node.label}</span><small>{node.path}</small></button>)}</div>
              <div className="ex-graph-edges" aria-label="知识关系">{graph.edges.length ? graph.edges.map((edge) => <button className="ex-button" type="button" key={`${edge.source}->${edge.target}`} onClick={() => void loadDocument(edge.target)}><span>{edge.source}</span><ChevronRight aria-hidden="true" /><span>{edge.target}</span></button>) : <p>当前文档之间没有内部链接。</p>}</div>
            </>
          ) : <p className="ex-content-empty">{loading ? "正在构建关系图…" : "暂无可展示的知识关系。"}</p>}
        </div>
      )}
    </section>
  );
}


interface MemorySettingsProps {
  active: boolean;
  client: RuntimeClient;
}

export function MemorySettings({
  active,
  client,
}: MemorySettingsProps) {
  const [view, setView] = useState<MemoryContentView>("files");
  const [page, setPage] = useState(1);
  const [contentPage, setContentPage] = useState<MemoryContentPage | null>(null);
  const [document, setDocument] = useState<MemoryContentDocument | null>(null);
  const [loading, setLoading] = useState(false);
  const [contentError, setContentError] = useState<string | null>(null);
  const [learning, setLearning] = useState<MemoryLearningSettings | null>(null);
  const [learningBusy, setLearningBusy] = useState(false);

  const loadPage = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setContentError(null);
    try {
      const result = await client.memoryContent(view, page, signal);
      if (!signal?.aborted) {
        setContentPage(result);
        setDocument(null);
      }
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) {
        setContentError(userFacingError(cause));
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [client, page, view]);

  const loadDocument = useCallback(async (itemId: string, signal?: AbortSignal) => {
    setLoading(true);
    setContentError(null);
    try {
      const result = await client.memoryContentDocument(view, itemId, signal);
      if (!signal?.aborted) setDocument(result);
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) {
        setContentError(userFacingError(cause));
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [client, view]);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void loadPage(controller.signal);
    void client.memoryLearningSettings(controller.signal)
      .then((value) => { if (!controller.signal.aborted) setLearning(value); })
      .catch((cause: unknown) => {
        if (!(cause instanceof DOMException && cause.name === "AbortError")) {
          setContentError(userFacingError(cause));
        }
      });
    return () => controller.abort();
  }, [active, client, loadPage]);

  const toggleLearning = async () => {
    if (!learning || learningBusy) return;
    setLearningBusy(true);
    setContentError(null);
    try {
      setLearning(await client.setMemoryLearningEnabled(!learning.enabled));
    } catch (cause) {
      setContentError(userFacingError(cause));
    } finally {
      setLearningBusy(false);
    }
  };

  const totalPages = Math.max(1, Math.ceil((contentPage?.total ?? 0) / 10));

  return (
    <section className="ex-settings-section ex-content-settings" id="settings-memory" hidden={!active}>
      <h2>记忆</h2>
      <div className="ex-content-heading">
        <p>{loading && !contentPage ? "正在读取记忆文件" : `${contentPage?.total ?? 0} 个记忆文件`}</p>
        <div className="ex-content-actions">
          <button className="ex-button" type="button" disabled={loading} onClick={() => void loadPage()}><RefreshCw aria-hidden="true" />刷新</button>
        </div>
      </div>
      <p className="ex-settings-note">直接读取工作区中的 MEMORY.md、每日记忆和进化记录；Agent 与此页面使用同一份文件。</p>
      <div className="ex-settings-row">
        <div>
          <strong>记忆学习与夜间梦境</strong>
          <p>开启后按 Cow 的空闲触发和夜间整理规则学习；关闭后不调度、不执行。</p>
        </div>
        <button
          className="ex-skill-switch"
          type="button"
          role="switch"
          aria-label="记忆学习与夜间梦境"
          aria-checked={learning?.enabled === true}
          aria-busy={learningBusy}
          disabled={!learning || learningBusy}
          onClick={() => void toggleLearning()}
        ><span /></button>
      </div>
      {contentError ? <div className="ex-settings-error" role="alert"><span>{contentError}</span><button className="ex-button" type="button" onClick={() => setContentError(null)}>关闭</button></div> : null}
      <div className="ex-content-tabs" role="tablist" aria-label="记忆视图">
        <button className="ex-button" type="button" role="tab" aria-selected={view === "files"} onClick={() => { setView("files"); setPage(1); }}>记忆文件</button>
        <button className="ex-button" type="button" role="tab" aria-selected={view === "evolution"} onClick={() => { setView("evolution"); setPage(1); }}>进化记录</button>
      </div>
      <div className="ex-memory-browser">
        <div className="ex-memory-list">
          <div className="ex-memory-list-head"><span>名称</span><span>类型</span><span>大小</span><span>更新时间</span></div>
          {contentPage?.items.length ? contentPage.items.map((item) => (
            <button className="ex-button" type="button" key={item.item_id} aria-current={document?.item_id === item.item_id ? "true" : undefined} onClick={() => void loadDocument(item.item_id)}>
              <span><FileText aria-hidden="true" /><span><strong>{item.name}</strong><small>{item.path}</small></span></span>
              <span>{item.origin === "factory" ? "内置" : item.origin === "imported" ? "导入" : "学习"}</span>
              <span>{formatBytes(item.size_bytes)}</span>
              <span>{formatTime(item.updated_at)}</span>
            </button>
          )) : <p className="ex-content-empty">{loading ? "正在读取记忆…" : "当前没有活动记忆。"}</p>}
          <nav className="ex-content-pagination" aria-label="记忆分页"><button className="ex-button" type="button" aria-label="上一页" disabled={page <= 1 || loading} onClick={() => setPage((value) => Math.max(1, value - 1))}><ChevronLeft aria-hidden="true" /></button><span>第 {page} / {totalPages} 页</span><button className="ex-button" type="button" aria-label="下一页" disabled={page >= totalPages || loading} onClick={() => setPage((value) => value + 1)}><ChevronRight aria-hidden="true" /></button></nav>
        </div>
        <article className="ex-content-preview" aria-label="记忆内容">
          {document ? <><header><div><h3>{document.name}</h3><p>{document.path} · {formatBytes(document.size_bytes)} · {formatTime(document.updated_at)}</p></div></header><div className="ex-content-markdown"><ReactMarkdown skipHtml remarkPlugins={[remarkGfm]} components={{ img: ({ alt = "" }) => <span>{alt}</span>, a: ({ href = "", children }) => { const external = httpsLink(href); return external ? <a href={external} target="_blank" rel="noreferrer">{children}</a> : <span>{children}</span>; } }}>{document.content}</ReactMarkdown></div></> : <p className="ex-content-empty">从左侧选择一项记忆查看内容。</p>}
        </article>
      </div>
    </section>
  );
}
