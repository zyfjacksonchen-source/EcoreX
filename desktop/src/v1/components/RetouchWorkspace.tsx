import * as Dialog from "@radix-ui/react-dialog";
import {
  Brush,
  Circle,
  GitCompareArrows,
  Hand,
  Image as ImageIcon,
  MapPin,
  Maximize2,
  MousePointer2,
  Pentagon,
  Plus,
  Redo2,
  Send,
  Square,
  Trash2,
  Undo2,
  Waypoints,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from "react";

import type {
  ArtifactProjection,
  RetouchAnnotation,
  RetouchPoint,
  RetouchViewState,
  RetouchWorkspaceProjection,
} from "../api/contracts.ts";
import { RuntimeApiError } from "../api/runtimeClient.ts";
import { serviceReasonMessage, userFacingError } from "../state/userLanguage.ts";
import {
  annotationAt,
  boundedHistory,
  boxBetween,
  clamp,
  normalizedViewBox,
  translateAnnotation,
} from "../state/retouchCanvas.ts";
import { IconButton } from "./IconButton.tsx";

type Tool = RetouchViewState["tool"];
type DrawingTool = Exclude<Tool, "select" | "pan">;
type ReviewMode = "result" | "original" | "compare";

interface DraftShape {
  tool: DrawingTool;
  points: RetouchPoint[];
}

type PointerSession =
  | { kind: "box"; pointerId: number; start: RetouchPoint; tool: "rectangle" | "ellipse" }
  | { kind: "brush"; pointerId: number; points: RetouchPoint[] }
  | {
      kind: "move";
      pointerId: number;
      start: RetouchPoint;
      annotationId: string;
      original: RetouchAnnotation[];
    }
  | {
      kind: "pan";
      pointerId: number;
      clientX: number;
      clientY: number;
      original: RetouchViewState;
    };

interface RetouchWorkspaceProps {
  artifact: ArtifactProjection | null;
  artifacts: ArtifactProjection[];
  artifactPreviewUrls: Record<string, string>;
  onClose: () => void;
  onOpenWorkspace: (artifact: ArtifactProjection) => Promise<RetouchWorkspaceProjection>;
  onGetWorkspace: (
    workspaceId: string,
    signal?: AbortSignal,
  ) => Promise<RetouchWorkspaceProjection>;
  onSaveWorkspace: (
    workspace: RetouchWorkspaceProjection,
    input: {
      annotations: RetouchAnnotation[];
      referenceArtifactIds: string[];
      globalInstruction: string;
      viewState: Partial<RetouchViewState>;
    },
  ) => Promise<RetouchWorkspaceProjection>;
  onSubmitWorkspace: (
    workspace: RetouchWorkspaceProjection,
  ) => Promise<RetouchWorkspaceProjection>;
  onReopenWorkspace: (
    workspace: RetouchWorkspaceProjection,
  ) => Promise<RetouchWorkspaceProjection>;
  onLoadBlob: (
    workspaceId: string,
    kind: "surface" | "result" | "reference",
    referenceArtifactId?: string,
    signal?: AbortSignal,
  ) => Promise<Blob>;
  onOpenResult: (artifact: ArtifactProjection) => void;
  onContinueResult: (artifact: ArtifactProjection) => void;
}

interface EditorSnapshot {
  annotations: RetouchAnnotation[];
  referenceArtifactIds: string[];
  globalInstruction: string;
  view: RetouchViewState;
}

const DEFAULT_VIEW: RetouchViewState = {
  zoom: 1,
  pan_x: 0.5,
  pan_y: 0.5,
  selected_annotation_id: null,
  tool: "rectangle",
};

function errorText(error: unknown): string {
  return userFacingError(error);
}

function annotationId(): string {
  return `ann_${crypto.randomUUID().replaceAll("-", "")}`;
}

function svgPoint(event: ReactPointerEvent<SVGSVGElement>): RetouchPoint {
  const svg = event.currentTarget;
  const matrix = svg.getScreenCTM();
  if (matrix) {
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const mapped = point.matrixTransform(matrix.inverse());
    return { x: clamp(mapped.x), y: clamp(mapped.y) };
  }
  const bounds = svg.getBoundingClientRect();
  return {
    x: clamp((event.clientX - bounds.left) / Math.max(1, bounds.width)),
    y: clamp((event.clientY - bounds.top) / Math.max(1, bounds.height)),
  };
}

function mergeView(value: Partial<RetouchViewState>): RetouchViewState {
  return {
    zoom: clamp(Number(value.zoom ?? 1), 1, 8),
    pan_x: clamp(Number(value.pan_x ?? 0.5)),
    pan_y: clamp(Number(value.pan_y ?? 0.5)),
    selected_annotation_id: value.selected_annotation_id ?? null,
    tool: value.tool ?? "rectangle",
  };
}

function pointsAttribute(points: RetouchPoint[]): string {
  return points.map((point) => `${point.x},${point.y}`).join(" ");
}

function toolLabel(tool: Tool): string {
  return {
    select: "选择和移动",
    rectangle: "矩形区域",
    ellipse: "椭圆区域",
    point: "单点标记",
    polygon: "多边形区域",
    polyline: "折线标记",
    brush: "画笔标记",
    pan: "平移画布",
  }[tool];
}

function jobStatus(workspace: RetouchWorkspaceProjection | null): string | null {
  const job = workspace?.job;
  if (!job) return null;
  if (job.status === "queued") return "修图任务已排队，草稿和原图修订已固定。";
  if (job.status === "running") return "正在生成新修订；可以关闭窗口，任务会继续运行。";
  if (job.status === "completed") return "新修订已完成。请对比原图并检查标记区域。";
  if (job.status === "failed") {
    return `修图失败：${serviceReasonMessage(
      job.failure_reason,
      "图片服务没有返回可用结果。请稍后重试。",
    )}`;
  }
  return "修图任务已取消，当前草稿仍保留在后台。";
}

export function RetouchWorkspace({
  artifact,
  artifacts,
  artifactPreviewUrls,
  onClose,
  onOpenWorkspace,
  onGetWorkspace,
  onSaveWorkspace,
  onSubmitWorkspace,
  onReopenWorkspace,
  onLoadBlob,
  onOpenResult,
  onContinueResult,
}: RetouchWorkspaceProps) {
  const [workspace, setWorkspace] = useState<RetouchWorkspaceProjection | null>(null);
  const [annotations, setAnnotations] = useState<RetouchAnnotation[]>([]);
  const [referenceArtifactIds, setReferenceArtifactIds] = useState<string[]>([]);
  const [globalInstruction, setGlobalInstruction] = useState("");
  const [regionInstruction, setRegionInstruction] = useState("");
  const [view, setView] = useState<RetouchViewState>(DEFAULT_VIEW);
  const [past, setPast] = useState<RetouchAnnotation[][]>([]);
  const [future, setFuture] = useState<RetouchAnnotation[][]>([]);
  const [draftShape, setDraftShape] = useState<DraftShape | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [closing, setClosing] = useState(false);
  const [saveState, setSaveState] = useState<"saved" | "pending" | "saving" | "error">("saved");
  const [error, setError] = useState<string | null>(null);
  const [surfaceUrl, setSurfaceUrl] = useState<string | null>(null);
  const [resultUrl, setResultUrl] = useState<string | null>(null);
  const [referenceUrls, setReferenceUrls] = useState<Record<string, string>>({});
  const [referenceCandidate, setReferenceCandidate] = useState("");
  const [reviewMode, setReviewMode] = useState<ReviewMode>("result");
  const pointerSession = useRef<PointerSession | null>(null);
  const instructionBaseline = useRef<RetouchAnnotation[] | null>(null);
  const surfaceUrlRef = useRef<string | null>(null);
  const resultUrlRef = useRef<string | null>(null);
  const referenceUrlsRef = useRef<Record<string, string>>({});
  const workspaceRef = useRef<RetouchWorkspaceProjection | null>(null);
  const editorRef = useRef<EditorSnapshot>({
    annotations: [],
    referenceArtifactIds: [],
    globalInstruction: "",
    view: DEFAULT_VIEW,
  });
  const localRevision = useRef(0);
  const savedRevision = useRef(0);
  const savePromise = useRef<Promise<RetouchWorkspaceProjection | null> | null>(null);
  const [saveTick, setSaveTick] = useState(0);

  useEffect(() => {
    editorRef.current = { annotations, referenceArtifactIds, globalInstruction, view };
  }, [annotations, globalInstruction, referenceArtifactIds, view]);

  useEffect(() => { surfaceUrlRef.current = surfaceUrl; }, [surfaceUrl]);
  useEffect(() => { resultUrlRef.current = resultUrl; }, [resultUrl]);
  useEffect(() => { referenceUrlsRef.current = referenceUrls; }, [referenceUrls]);

  const setServerWorkspace = useCallback((next: RetouchWorkspaceProjection) => {
    workspaceRef.current = next;
    setWorkspace(next);
  }, []);

  const markDirty = useCallback(() => {
    localRevision.current += 1;
    setSaveState("pending");
    setSaveTick((value) => value + 1);
  }, []);

  const resetFromProjection = useCallback((next: RetouchWorkspaceProjection) => {
    setServerWorkspace(next);
    setAnnotations(next.annotations);
    setReferenceArtifactIds(next.references.map((item) => item.artifact_id));
    setGlobalInstruction(next.global_instruction);
    const nextView = mergeView(next.view_state);
    setView(nextView);
    editorRef.current = {
      annotations: next.annotations,
      referenceArtifactIds: next.references.map((item) => item.artifact_id),
      globalInstruction: next.global_instruction,
      view: nextView,
    };
    localRevision.current = 0;
    savedRevision.current = 0;
    setPast([]);
    setFuture([]);
    setSaveState("saved");
  }, [setServerWorkspace]);

  useEffect(() => {
    if (!artifact) return;
    const controller = new AbortController();
    let stale = false;
    setLoading(true);
    setError(null);
    if (surfaceUrlRef.current) URL.revokeObjectURL(surfaceUrlRef.current);
    if (resultUrlRef.current) URL.revokeObjectURL(resultUrlRef.current);
    surfaceUrlRef.current = null;
    resultUrlRef.current = null;
    setSurfaceUrl(null);
    setResultUrl(null);
    void (async () => {
      try {
        const opened = await onOpenWorkspace(artifact);
        if (stale) return;
        resetFromProjection(opened);
        const blob = await onLoadBlob(opened.workspace_id, "surface", undefined, controller.signal);
        if (stale) return;
        const next = URL.createObjectURL(blob);
        surfaceUrlRef.current = next;
        setSurfaceUrl(next);
      } catch (cause) {
        if (!stale) setError(errorText(cause));
      } finally {
        if (!stale) setLoading(false);
      }
    })();
    return () => {
      stale = true;
      controller.abort();
    };
  }, [artifact?.artifact_id, artifact?.revision_id, onLoadBlob, onOpenWorkspace, resetFromProjection]);

  useEffect(() => () => {
    if (surfaceUrlRef.current) URL.revokeObjectURL(surfaceUrlRef.current);
    if (resultUrlRef.current) URL.revokeObjectURL(resultUrlRef.current);
    Object.values(referenceUrlsRef.current).forEach((url) => URL.revokeObjectURL(url));
  }, []);

  const flushSave = useCallback(async (): Promise<RetouchWorkspaceProjection | null> => {
    if (savePromise.current) return savePromise.current;
    const execute = async () => {
      let current = workspaceRef.current;
      if (!current) return null;
      while (
        current.status === "editing"
        && savedRevision.current < localRevision.current
      ) {
        const generation = localRevision.current;
        const editor = editorRef.current;
        setSaveState("saving");
        try {
          current = await onSaveWorkspace(current, {
            annotations: editor.annotations,
            referenceArtifactIds: editor.referenceArtifactIds,
            globalInstruction: editor.globalInstruction,
            viewState: editor.view,
          });
        } catch (cause) {
          if (cause instanceof RuntimeApiError && cause.status === 409) {
            const refreshed = await onGetWorkspace(current.workspace_id);
            setServerWorkspace(refreshed);
            setSaveState("error");
            throw new Error("草稿已在另一窗口更新；本窗口的未保存修改仍保留，请关闭另一窗口后重试。");
          }
          setSaveState("error");
          throw cause;
        }
        setServerWorkspace(current);
        savedRevision.current = generation;
      }
      setSaveState("saved");
      return current;
    };
    savePromise.current = execute().finally(() => {
      savePromise.current = null;
    });
    return savePromise.current;
  }, [onGetWorkspace, onSaveWorkspace, setServerWorkspace]);

  useEffect(() => {
    if (!workspace || workspace.status !== "editing") return;
    if (savedRevision.current >= localRevision.current) return;
    const timer = window.setTimeout(() => {
      void flushSave().catch((cause) => setError(errorText(cause)));
    }, 450);
    return () => window.clearTimeout(timer);
  }, [flushSave, saveTick, workspace]);

  useEffect(() => {
    if (artifact || workspaceRef.current?.status !== "editing") return;
    void flushSave().catch(() => undefined);
  }, [artifact, flushSave]);

  useEffect(() => () => {
    if (
      workspaceRef.current?.status === "editing"
      && savedRevision.current < localRevision.current
    ) {
      void flushSave().catch(() => undefined);
    }
  }, [flushSave]);

  useEffect(() => {
    if (!workspace?.submitted_job_id) return;
    if (workspace.job?.status === "completed" || workspace.job?.status === "failed" || workspace.job?.status === "cancelled") return;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const next = await onGetWorkspace(workspace.workspace_id, controller.signal);
        if (!controller.signal.aborted) setServerWorkspace(next);
      } catch (cause) {
        if (!controller.signal.aborted) setError(errorText(cause));
      }
    };
    const timer = window.setInterval(() => void poll(), 1200);
    void poll();
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [onGetWorkspace, setServerWorkspace, workspace?.job?.status, workspace?.submitted_job_id, workspace?.workspace_id]);

  useEffect(() => {
    if (!workspace?.result || !workspace.result_url) return;
    const controller = new AbortController();
    void onLoadBlob(workspace.workspace_id, "result", undefined, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return;
        const next = URL.createObjectURL(blob);
        if (resultUrlRef.current) URL.revokeObjectURL(resultUrlRef.current);
        resultUrlRef.current = next;
        setResultUrl(next);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorText(cause));
      });
    return () => controller.abort();
  }, [onLoadBlob, workspace?.result?.revision_id, workspace?.workspace_id]);

  useEffect(() => {
    if (!workspace) return;
    const controller = new AbortController();
    const exactReferences = workspace.references.filter((reference) => (
      referenceArtifactIds.includes(reference.artifact_id)
    ));
    void Promise.all(exactReferences.map(async (reference) => {
      const blob = await onLoadBlob(
        workspace.workspace_id,
        "reference",
        reference.artifact_id,
        controller.signal,
      );
      return [reference.artifact_id, URL.createObjectURL(blob)] as const;
    })).then((entries) => {
      if (controller.signal.aborted) {
        entries.forEach(([, url]) => URL.revokeObjectURL(url));
        return;
      }
      Object.values(referenceUrlsRef.current).forEach((url) => URL.revokeObjectURL(url));
      const next = Object.fromEntries(entries);
      referenceUrlsRef.current = next;
      setReferenceUrls(next);
    }).catch((cause) => {
      if (!controller.signal.aborted) setError(errorText(cause));
    });
    return () => controller.abort();
  }, [onLoadBlob, referenceArtifactIds.join("|"), workspace?.references, workspace?.workspace_id]);

  const selected = annotations.find((item) => item.annotation_id === view.selected_annotation_id) ?? null;
  const availableReferences = useMemo(() => artifacts.filter((item) => (
    item.family === "image"
    && item.status === "ready"
    && item.artifact_id !== artifact?.artifact_id
    && !referenceArtifactIds.includes(item.artifact_id)
  )), [artifact?.artifact_id, artifacts, referenceArtifactIds]);

  const commitAnnotations = useCallback((next: RetouchAnnotation[], previous = annotations) => {
    setPast((items) => boundedHistory(items, previous));
    setFuture([]);
    setAnnotations(next);
    markDirty();
  }, [annotations, markDirty]);

  const addAnnotation = useCallback((annotation: RetouchAnnotation) => {
    commitAnnotations([...annotations, annotation]);
    setView((current) => ({ ...current, selected_annotation_id: annotation.annotation_id ?? null, tool: "select" }));
  }, [annotations, commitAnnotations]);

  const undo = useCallback(() => {
    const previous = past.at(-1);
    if (!previous) return;
    setPast((items) => items.slice(0, -1));
    setFuture((items) => boundedHistory(items, annotations));
    setAnnotations(previous);
    setView((current) => ({ ...current, selected_annotation_id: null }));
    markDirty();
  }, [annotations, markDirty, past]);

  const redo = useCallback(() => {
    const next = future.at(-1);
    if (!next) return;
    setFuture((items) => items.slice(0, -1));
    setPast((items) => boundedHistory(items, annotations));
    setAnnotations(next);
    setView((current) => ({ ...current, selected_annotation_id: null }));
    markDirty();
  }, [annotations, future, markDirty]);

  const removeSelected = useCallback(() => {
    if (!view.selected_annotation_id) return;
    commitAnnotations(annotations.filter((item) => item.annotation_id !== view.selected_annotation_id));
    setView((current) => ({ ...current, selected_annotation_id: null }));
  }, [annotations, commitAnnotations, view.selected_annotation_id]);

  const finishPath = useCallback(() => {
    if (!draftShape || (draftShape.tool !== "polygon" && draftShape.tool !== "polyline")) return;
    const minimum = draftShape.tool === "polygon" ? 3 : 2;
    if (draftShape.points.length < minimum) {
      setError(`${toolLabel(draftShape.tool)}至少需要 ${minimum} 个点。`);
      return;
    }
    addAnnotation({
      annotation_id: annotationId(),
      kind: draftShape.tool,
      normalized_geometry: { points: draftShape.points },
      instruction: regionInstruction.trim() || "请按标记范围进行局部修改",
    });
    setDraftShape(null);
    setRegionInstruction("");
  }, [addAnnotation, draftShape, regionInstruction]);

  const changeView = useCallback((next: RetouchViewState, persist = true) => {
    setView(next);
    if (persist) markDirty();
  }, [markDirty]);

  const cancelActiveGesture = useCallback(() => {
    const session = pointerSession.current;
    if (session?.kind === "move") setAnnotations(session.original);
    if (session?.kind === "pan") setView(session.original);
    pointerSession.current = null;
    setDraftShape(null);
  }, []);

  const pointerDown = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (workspace?.status !== "editing" || event.button !== 0) return;
    const point = svgPoint(event);
    const tool = view.tool;
    setError(null);
    if (tool === "point") {
      addAnnotation({
        annotation_id: annotationId(),
        kind: "point",
        normalized_geometry: point,
        instruction: regionInstruction.trim() || "请修改此标记位置",
      });
      setRegionInstruction("");
      return;
    }
    if (tool === "polygon" || tool === "polyline") {
      setDraftShape((current) => current?.tool === tool
        ? { ...current, points: [...current.points, point] }
        : { tool, points: [point] });
      return;
    }
    event.currentTarget.setPointerCapture(event.pointerId);
    if (tool === "rectangle" || tool === "ellipse") {
      pointerSession.current = { kind: "box", pointerId: event.pointerId, start: point, tool };
      setDraftShape({ tool, points: [point, point] });
      return;
    }
    if (tool === "brush") {
      pointerSession.current = { kind: "brush", pointerId: event.pointerId, points: [point] };
      setDraftShape({ tool, points: [point] });
      return;
    }
    if (tool === "pan") {
      pointerSession.current = {
        kind: "pan",
        pointerId: event.pointerId,
        clientX: event.clientX,
        clientY: event.clientY,
        original: view,
      };
      return;
    }
    const hit = annotationAt(annotations, point);
    setView((current) => ({ ...current, selected_annotation_id: hit?.annotation_id ?? null }));
    markDirty();
    if (hit?.annotation_id) {
      pointerSession.current = {
        kind: "move",
        pointerId: event.pointerId,
        start: point,
        annotationId: hit.annotation_id,
        original: annotations,
      };
    }
  };

  const pointerMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    const session = pointerSession.current;
    if (!session || session.pointerId !== event.pointerId) return;
    const point = svgPoint(event);
    if (session.kind === "box") {
      setDraftShape({ tool: session.tool, points: [session.start, point] });
    } else if (session.kind === "brush") {
      const last = session.points.at(-1) ?? point;
      if (Math.hypot(point.x - last.x, point.y - last.y) >= 0.003) {
        session.points.push(point);
        setDraftShape({ tool: "brush", points: [...session.points] });
      }
    } else if (session.kind === "move") {
      const delta = { x: point.x - session.start.x, y: point.y - session.start.y };
      setAnnotations(session.original.map((item) => (
        item.annotation_id === session.annotationId ? translateAnnotation(item, delta) : item
      )));
    } else {
      const bounds = event.currentTarget.getBoundingClientRect();
      const dx = (event.clientX - session.clientX) / Math.max(bounds.width, 1) / session.original.zoom;
      const dy = (event.clientY - session.clientY) / Math.max(bounds.height, 1) / session.original.zoom;
      setView({
        ...session.original,
        pan_x: clamp(session.original.pan_x - dx),
        pan_y: clamp(session.original.pan_y - dy),
      });
    }
  };

  const endPointer = (event: ReactPointerEvent<SVGSVGElement>, cancelled = false) => {
    const session = pointerSession.current;
    if (!session || session.pointerId !== event.pointerId) return;
    pointerSession.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (cancelled) {
      if (session.kind === "move") setAnnotations(session.original);
      if (session.kind === "pan") setView(session.original);
      setDraftShape(null);
      return;
    }
    if (session.kind === "box") {
      const bounds = boxBetween(session.start, svgPoint(event));
      if (bounds.width >= 0.005 && bounds.height >= 0.005) {
        addAnnotation({
          annotation_id: annotationId(),
          kind: session.tool,
          normalized_geometry: bounds,
          instruction: regionInstruction.trim() || "请按标记范围进行局部修改",
        });
        setRegionInstruction("");
      }
      setDraftShape(null);
    } else if (session.kind === "brush") {
      if (session.points.length >= 2) {
        addAnnotation({
          annotation_id: annotationId(),
          kind: "brush",
          normalized_geometry: { points: session.points, width: 0.012 },
          instruction: regionInstruction.trim() || "请沿画笔标记进行局部修改",
        });
        setRegionInstruction("");
      }
      setDraftShape(null);
    } else if (session.kind === "move") {
      setPast((items) => boundedHistory(items, session.original));
      setFuture([]);
      markDirty();
    } else {
      markDirty();
    }
  };

  const submit = useCallback(async () => {
    if (!workspaceRef.current || (!annotations.length && !globalInstruction.trim())) return;
    setSubmitting(true);
    setError(null);
    try {
      const saved = await flushSave();
      if (!saved) throw new Error("修图草稿尚未就绪。");
      const submitted = await onSubmitWorkspace(saved);
      setServerWorkspace(submitted);
    } catch (cause) {
      setError(errorText(cause));
      try {
        const current = workspaceRef.current;
        if (current) {
          const recovered = await onGetWorkspace(current.workspace_id);
          setServerWorkspace(recovered);
          if (recovered.status === "submitted") setError(null);
        }
      } catch {
        // The local editor state remains intact even when recovery cannot reach Runtime.
      }
    } finally {
      setSubmitting(false);
    }
  }, [annotations.length, flushSave, globalInstruction, onGetWorkspace, onSubmitWorkspace, setServerWorkspace]);

  const requestClose = useCallback(async () => {
    if (closing) return;
    setClosing(true);
    try {
      if (workspaceRef.current?.status === "editing") await flushSave();
      onClose();
    } catch (cause) {
      setError(`关闭前未能保存草稿：${errorText(cause)}`);
    } finally {
      setClosing(false);
    }
  }, [closing, flushSave, onClose]);

  const keyboard = (event: ReactKeyboardEvent<HTMLElement>) => {
    const command = event.ctrlKey || event.metaKey;
    const eventTarget = event.target as HTMLElement;
    const formControl = ["input", "textarea", "select"].includes(
      eventTarget.tagName.toLowerCase(),
    );
    if (command && event.key === "Enter") {
      event.preventDefault();
      void submit();
      return;
    }
    if (command && event.key.toLowerCase() === "z") {
      if (formControl) return;
      event.preventDefault();
      if (event.shiftKey) redo(); else undo();
      return;
    }
    if (event.key === "Escape") {
      if (draftShape || pointerSession.current) {
        event.preventDefault();
        cancelActiveGesture();
      } else {
        event.preventDefault();
        void requestClose();
      }
      return;
    }
    if (!formControl && event.key === "Enter" && draftShape && ["polygon", "polyline"].includes(draftShape.tool)) {
      event.preventDefault();
      finishPath();
      return;
    }
    if (
      (event.key === "Delete" || event.key === "Backspace")
      && eventTarget.tagName.toLowerCase() === "svg"
    ) {
      event.preventDefault();
      removeSelected();
    }
  };

  const stageStyle = workspace ? ({
    "--retouch-stage-aspect": `${workspace.edit_surface.width_px} / ${workspace.edit_surface.height_px}`,
  } as CSSProperties) : undefined;
  const resultStageStyle = workspace?.result_surface ? ({
    "--retouch-stage-aspect": `${workspace.result_surface.width_px} / ${workspace.result_surface.height_px}`,
  } as CSSProperties) : undefined;

  const renderAnnotation = (annotation: RetouchAnnotation, index: number) => {
    const selectedClass = annotation.annotation_id === view.selected_annotation_id ? " is-selected" : "";
    const className = `ex-retouch-shape is-saved${selectedClass}`;
    let shape;
    if (annotation.kind === "rectangle") {
      shape = <rect className={className} {...annotation.normalized_geometry} />;
    } else if (annotation.kind === "ellipse") {
      const box = annotation.normalized_geometry;
      shape = <ellipse className={className} cx={box.x + box.width / 2} cy={box.y + box.height / 2} rx={box.width / 2} ry={box.height / 2} />;
    } else if (annotation.kind === "point") {
      shape = <circle className={className} cx={annotation.normalized_geometry.x} cy={annotation.normalized_geometry.y} r={0.012} />;
    } else if (annotation.kind === "polygon") {
      shape = <polygon className={className} points={pointsAttribute(annotation.normalized_geometry.points)} />;
    } else {
      shape = <polyline className={className} points={pointsAttribute(annotation.normalized_geometry.points)} />;
    }
    return <g key={annotation.annotation_id}>{shape}<text x={annotation.kind === "point" ? annotation.normalized_geometry.x + 0.015 : 0.015} y={annotation.kind === "point" ? annotation.normalized_geometry.y - 0.015 : 0.035}>{index + 1}</text></g>;
  };

  const inspectionOverlay = workspace?.result_surface && workspace?.job?.inspection_regions.length ? (
    <svg className="ex-retouch-inspection-overlay" viewBox="0 0 1 1" preserveAspectRatio="none" aria-label="结果检查区域">
      {workspace.job.inspection_regions.map((region, index) => {
        const geometry = region.normalized_geometry;
        if ("points" in geometry) {
          return <polygon key={`${region.summary}-${index}`} points={pointsAttribute(geometry.points)}><title>{region.summary}</title></polygon>;
        }
        if ("height" in geometry) {
          return <rect key={`${region.summary}-${index}`} x={geometry.x} y={geometry.y} width={geometry.width} height={geometry.height}><title>{region.summary}</title></rect>;
        }
        return <circle key={`${region.summary}-${index}`} cx={geometry.x} cy={geometry.y} r={0.015}><title>{region.summary}</title></circle>;
      })}
    </svg>
  ) : null;

  const status = jobStatus(workspace);
  const selectedReferenceArtifacts = referenceArtifactIds.map((id) => (
    artifacts.find((item) => item.artifact_id === id)
  )).filter((item): item is ArtifactProjection => Boolean(item));

  return (
    <Dialog.Root open={artifact !== null} onOpenChange={(open) => { if (!open) void requestClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="ex-dialog-overlay" />
        <Dialog.Content
          className="ex-dialog ex-retouch-dialog"
          aria-describedby={undefined}
          onEscapeKeyDown={(event) => event.preventDefault()}
          onKeyDown={keyboard}
        >
          <div className="ex-dialog-heading">
            <div>
              <Dialog.Title>精准修图</Dialog.Title>
              <span>{artifact?.display_name} · 修订 {workspace?.edit_surface.base_revision_id.slice(-8) ?? "加载中"}</span>
            </div>
            <IconButton label="保存并关闭精准修图" disabled={closing} onClick={() => void requestClose()}>
              <X aria-hidden="true" />
            </IconButton>
          </div>

          {error ? <div className="ex-retouch-status is-error" role="alert">{error}</div> : null}
          {status ? <div className={`ex-retouch-status${workspace?.job?.status === "failed" ? " is-error" : ""}`} role="status">{status}</div> : null}

          <div className="ex-retouch-workspace">
            <section className="ex-retouch-canvas-column" aria-label="修图画布">
              {workspace?.result && resultUrl ? (
                <>
                  <div className="ex-retouch-review-tabs" role="group" aria-label="结果查看方式">
                    <button type="button" className={reviewMode === "result" ? "is-active" : ""} onClick={() => setReviewMode("result")}><ImageIcon aria-hidden="true" />新修订</button>
                    <button type="button" className={reviewMode === "original" ? "is-active" : ""} onClick={() => setReviewMode("original")}>原图</button>
                    <button type="button" className={reviewMode === "compare" ? "is-active" : ""} onClick={() => setReviewMode("compare")}><GitCompareArrows aria-hidden="true" />并排对比</button>
                  </div>
                  <div className={`ex-retouch-review is-${reviewMode}`}>
                    {reviewMode !== "result" ? <figure><div className="ex-retouch-review-media" style={stageStyle}><img src={surfaceUrl ?? ""} alt="修图前的原始修订" /></div><figcaption>原图 · {workspace.edit_surface.base_revision_id.slice(-8)}</figcaption></figure> : null}
                    {reviewMode !== "original" ? <figure><div className="ex-retouch-review-media" style={resultStageStyle}><img src={resultUrl} alt="精准修图后的新修订" />{inspectionOverlay}</div><figcaption>新修订 · {workspace.result.revision_id.slice(-8)}</figcaption></figure> : null}
                  </div>
                  <div className="ex-retouch-result-summary">
                    <strong>{workspace.job?.change_summary || "新修订已生成"}</strong>
                    <span>{workspace.job?.inspection_regions.length ?? 0} 个检查区域</span>
                    <div>
                      <button className="ex-button" type="button" onClick={() => onOpenResult(workspace.result!)}>打开新修订</button>
                      <button className="ex-button is-primary" type="button" onClick={() => onContinueResult(workspace.result!)}>继续修改</button>
                    </div>
                  </div>
                </>
              ) : (
                <>
                  <div className="ex-retouch-toolbar" role="toolbar" aria-label="标注和画布工具">
                    {([
                      ["select", MousePointer2],
                      ["rectangle", Square],
                      ["ellipse", Circle],
                      ["point", MapPin],
                      ["polygon", Pentagon],
                      ["polyline", Waypoints],
                      ["brush", Brush],
                      ["pan", Hand],
                    ] as const).map(([tool, ToolIcon]) => (
                      <IconButton key={tool} label={toolLabel(tool)} className={view.tool === tool ? "is-active" : ""} onClick={() => changeView({ ...view, tool })}>
                        <ToolIcon aria-hidden="true" />
                      </IconButton>
                    ))}
                    <span className="ex-retouch-toolbar-separator" aria-hidden="true" />
                    <IconButton label="撤销" disabled={!past.length} onClick={undo}><Undo2 aria-hidden="true" /></IconButton>
                    <IconButton label="重做" disabled={!future.length} onClick={redo}><Redo2 aria-hidden="true" /></IconButton>
                    <IconButton label="缩小" disabled={view.zoom <= 1} onClick={() => changeView({ ...view, zoom: clamp(view.zoom / 1.4, 1, 8) })}><ZoomOut aria-hidden="true" /></IconButton>
                    <span className="ex-retouch-zoom-value">{Math.round(view.zoom * 100)}%</span>
                    <IconButton label="放大" disabled={view.zoom >= 8} onClick={() => changeView({ ...view, zoom: clamp(view.zoom * 1.4, 1, 8) })}><ZoomIn aria-hidden="true" /></IconButton>
                    <IconButton label="适合窗口" onClick={() => changeView({ ...view, zoom: 1, pan_x: 0.5, pan_y: 0.5 })}><Maximize2 aria-hidden="true" /></IconButton>
                  </div>
                  <div className="ex-retouch-stage">
                    {loading || !surfaceUrl || !workspace ? <p>正在加载固定修订画布…</p> : (
                      <div className="ex-retouch-stage-media" style={stageStyle}>
                        <svg
                          className="ex-retouch-overlay"
                          viewBox={normalizedViewBox(view)}
                          preserveAspectRatio="none"
                          tabIndex={0}
                          role="group"
                          aria-roledescription="图片标注画布"
                          aria-label={`${toolLabel(view.tool)}。在图片上标记需要修改的区域；按 Control 或 Command 加 Enter 提交。`}
                          onPointerDown={pointerDown}
                          onPointerMove={pointerMove}
                          onPointerUp={(event) => endPointer(event)}
                          onPointerCancel={(event) => endPointer(event, true)}
                          onLostPointerCapture={(event) => {
                            if (pointerSession.current?.pointerId === event.pointerId) {
                              cancelActiveGesture();
                            }
                          }}
                          onDoubleClick={finishPath}
                        >
                          <image href={surfaceUrl} x="0" y="0" width="1" height="1" preserveAspectRatio="none" />
                          {annotations.map(renderAnnotation)}
                          {draftShape?.tool === "rectangle" || draftShape?.tool === "ellipse" ? (() => {
                            const box = boxBetween(draftShape.points[0], draftShape.points[1]);
                            return draftShape.tool === "rectangle"
                              ? <rect className="ex-retouch-shape is-draft" {...box} />
                              : <ellipse className="ex-retouch-shape is-draft" cx={box.x + box.width / 2} cy={box.y + box.height / 2} rx={box.width / 2} ry={box.height / 2} />;
                          })() : null}
                          {draftShape && (draftShape.tool === "polygon" || draftShape.tool === "polyline" || draftShape.tool === "brush") ? (
                            draftShape.tool === "polygon"
                              ? <polygon className="ex-retouch-shape is-draft" points={pointsAttribute(draftShape.points)} />
                              : <polyline className="ex-retouch-shape is-draft" points={pointsAttribute(draftShape.points)} />
                          ) : null}
                        </svg>
                      </div>
                    )}
                  </div>
                </>
              )}
            </section>

            <aside className="ex-retouch-panel">
              <div className="ex-retouch-save-state" aria-live="polite">
                {saveState === "saved" ? "草稿已保存" : saveState === "saving" ? "正在保存草稿…" : saveState === "error" ? "草稿保存失败" : "有未保存修改"}
              </div>
              {workspace?.status === "editing" ? (
                <>
                  <label>
                    <span>新标记说明</span>
                    <textarea rows={2} value={regionInstruction} placeholder="先写要求，再在图上标记；也可标记后编辑" onChange={(event) => setRegionInstruction(event.target.value)} />
                  </label>
                  {draftShape && ["polygon", "polyline"].includes(draftShape.tool) ? (
                    <button className="ex-button" type="button" onClick={finishPath}>完成{toolLabel(draftShape.tool)}</button>
                  ) : null}
                  <div className="ex-retouch-region-list" aria-label="标注列表">
                    {annotations.length ? annotations.map((annotation, index) => (
                      <button
                        type="button"
                        key={annotation.annotation_id}
                        className={annotation.annotation_id === view.selected_annotation_id ? "is-selected" : ""}
                        onClick={() => changeView({ ...view, selected_annotation_id: annotation.annotation_id ?? null, tool: "select" })}
                      >
                        <span><strong>标注 {index + 1}</strong>{toolLabel(annotation.kind)}</span>
                        <small>{annotation.instruction}</small>
                      </button>
                    )) : <p>尚未添加标注；也可以只填写整体修改说明。</p>}
                  </div>
                  {selected ? (
                    <div className="ex-retouch-selection-editor">
                      <label>
                        <span>标注修改说明</span>
                        <textarea
                          rows={3}
                          value={selected.instruction}
                          onFocus={() => { instructionBaseline.current = annotations; }}
                          onChange={(event) => {
                            const next = annotations.map((item) => item.annotation_id === selected.annotation_id ? { ...item, instruction: event.target.value || "请修改此标记区域" } : item);
                            setAnnotations(next);
                            markDirty();
                          }}
                          onBlur={() => {
                            const baseline = instructionBaseline.current;
                            instructionBaseline.current = null;
                            if (baseline && JSON.stringify(baseline) !== JSON.stringify(annotations)) {
                              setPast((items) => boundedHistory(items, baseline));
                              setFuture([]);
                            }
                          }}
                        />
                      </label>
                      <button className="ex-button is-danger" type="button" onClick={removeSelected}><Trash2 aria-hidden="true" />删除标注</button>
                    </div>
                  ) : null}
                  <section className="ex-retouch-references" aria-labelledby="retouch-references-title">
                    <div>
                      <strong id="retouch-references-title">参考图</strong>
                      <span>{referenceArtifactIds.length}/10</span>
                    </div>
                    <div className="ex-retouch-reference-picker">
                      <select value={referenceCandidate} disabled={!availableReferences.length || referenceArtifactIds.length >= 10} onChange={(event) => setReferenceCandidate(event.target.value)} aria-label="选择参考图">
                        <option value="">选择当前会话中的图片</option>
                        {availableReferences.map((item) => <option key={item.artifact_id} value={item.artifact_id}>{item.display_name}</option>)}
                      </select>
                      <IconButton
                        label="添加参考图"
                        disabled={!referenceCandidate || referenceArtifactIds.length >= 10}
                        onClick={() => {
                          setReferenceArtifactIds((current) => [...current, referenceCandidate].slice(0, 10));
                          setReferenceCandidate("");
                          markDirty();
                        }}
                      ><Plus aria-hidden="true" /></IconButton>
                    </div>
                    <div className="ex-retouch-reference-list">
                      {selectedReferenceArtifacts.map((item) => (
                        <div key={item.artifact_id}>
                          {referenceUrls[item.artifact_id] || artifactPreviewUrls[item.artifact_id]
                            ? <img src={referenceUrls[item.artifact_id] || artifactPreviewUrls[item.artifact_id]} alt="" />
                            : <ImageIcon aria-hidden="true" />}
                          <span>{item.display_name}</span>
                          <IconButton label={`移除参考图 ${item.display_name}`} onClick={() => {
                            setReferenceArtifactIds((current) => current.filter((id) => id !== item.artifact_id));
                            markDirty();
                          }}><X aria-hidden="true" /></IconButton>
                        </div>
                      ))}
                    </div>
                  </section>
                  <label>
                    <span>整体修改说明</span>
                    <textarea
                      rows={4}
                      value={globalInstruction}
                      placeholder="可选：说明整体光线、风格或未标注区域的保持要求"
                      onChange={(event) => {
                        setGlobalInstruction(event.target.value);
                        markDirty();
                      }}
                    />
                  </label>
                  <button className="ex-button is-primary" type="button" disabled={submitting || (!annotations.length && !globalInstruction.trim())} onClick={() => void submit()}>
                    <Send aria-hidden="true" />
                    {submitting ? "正在提交…" : "开始修图"}
                  </button>
                  <small>Control/Command + Enter 提交 · Esc 取消当前绘制或保存关闭</small>
                </>
              ) : (
                <>
                  <strong>任务详情</strong>
                  <span>原图修订：{workspace?.edit_surface.base_revision_id.slice(-8)}</span>
                  <span>参考图：{workspace?.references.length ?? 0} 张</span>
                  <span>标注：{workspace?.annotations.length ?? 0} 个</span>
                  {workspace?.job?.inspection_regions.map((region, index) => (
                    <div className="ex-retouch-inspection" key={`${region.summary}-${index}`}><strong>检查 {index + 1}</strong><span>{region.summary}</span></div>
                  ))}
                  {workspace && (workspace.job?.status === "failed" || workspace.job?.status === "cancelled") ? (
                    <button className="ex-button is-primary" type="button" onClick={() => {
                      setError(null);
                      void onReopenWorkspace(workspace)
                        .then(resetFromProjection)
                        .catch((cause) => setError(errorText(cause)));
                    }}>保留草稿并重试</button>
                  ) : null}
                </>
              )}
            </aside>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
