import {
  ChevronDown,
  Circle,
  Copy,
  CornerDownLeft,
  Eraser,
  Hand,
  Image as ImageIcon,
  Maximize,
  Menu,
  MoreVertical,
  MousePointer2,
  PenLine,
  RotateCcw,
  SendHorizontal,
  Sparkles,
  Square,
  Trash2,
  Type,
  Upload,
  X,
  ZoomIn,
  ZoomOut
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ChangeEvent, type KeyboardEvent, type PointerEvent, type WheelEvent } from "react";

type Point = { x: number; y: number };

type Annotation = {
  id: string;
  kind: AnnotationKind;
  tip: Point;
  tail: Point;
  points?: Point[];
  text: string;
  color: string;
  strokeWidth: number;
  textSize: AnnotationTextSize;
  imageUrl?: string;
  imageName?: string;
};

type DraftAnnotation = {
  kind: AnnotationKind;
  tip: Point;
  tail: Point;
  points?: Point[];
};

type AnnotationTextSize = "S" | "M" | "L" | "XL";
type AnnotationKind = "arrow" | "rect" | "lasso" | "text" | "image";
type RetouchTool = "annotate" | "rect" | "lasso" | "text" | "hand";

type TextDraft = DraftAnnotation & {
  text: string;
  color: string;
  strokeWidth: number;
  textSize: AnnotationTextSize;
  imageUrl?: string;
  imageName?: string;
};

export type ImageRetouchRelatedImage = {
  key: string;
  title: string;
  sourcePath: string;
  previewUrl: string;
};

export type ImageRetouchSubmitPayload = {
  annotatedBlob: Blob;
  fileName: string;
  prompt: string;
  annotationCount: number;
  selectedSources: string[];
  selectedCount: number;
  textEditCount: number;
};

export type ImageRetouchCanvasProps = {
  title: string;
  sourcePath: string;
  imageUrl: string;
  relatedImages?: ImageRetouchRelatedImage[];
  busy?: boolean;
  error?: string;
  onClose: () => void;
  onSubmit: (payload: ImageRetouchSubmitPayload) => void | Promise<void>;
};

const RED = "#e53b38";
const TEXT_WIDTH = 260;
const TEXT_HEIGHT = 160;
const MIN_ARROW_DISTANCE = 10;
const MIN_ZOOM = 0.45;
const MAX_ZOOM = 2.5;
const ZOOM_STEP = 0.1;
const COLOR_SWATCHES = ["#111111", "#9aa3ad", "#cf6df2", "#a535d1", "#3d63e6", "#49a4ef", "#f4a642", "#e96b16", "#0f9b72", "#48a866", "#f87171", "#dc2626"];
const TEXT_SIZE_MAP: Record<AnnotationTextSize, number> = { S: 18, M: 23, L: 29, XL: 35 };

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}

function pointDistance(a: Point, b: Point) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function rectFromPoints(a: Point, b: Point) {
  const x = Math.min(a.x, b.x);
  const y = Math.min(a.y, b.y);
  return { x, y, width: Math.abs(a.x - b.x), height: Math.abs(a.y - b.y) };
}

function boundsFromPoints(points: Point[]) {
  if (!points.length) return { x: 0, y: 0, width: 0, height: 0 };
  const xs = points.map((point) => point.x);
  const ys = points.map((point) => point.y);
  const x = Math.min(...xs);
  const y = Math.min(...ys);
  return { x, y, width: Math.max(...xs) - x, height: Math.max(...ys) - y };
}

function lassoPath(points: Point[] = []) {
  if (!points.length) return "";
  const [first, ...rest] = points;
  return `M ${first.x} ${first.y} ${rest.map((point) => `L ${point.x} ${point.y}`).join(" ")} Z`;
}

function annotationKindLabel(kind: AnnotationKind) {
  if (kind === "rect") return "框选区域";
  if (kind === "lasso") return "圈选区域";
  if (kind === "text") return "文字修改";
  if (kind === "image") return "参考图片";
  return "箭头标注";
}

function annotationPrompt(annotations: Annotation[], extraInstruction: string, selectedSources: string[]) {
  const lines = annotations.map((item, index) => {
    const text = item.text.trim();
    if (!text && item.kind !== "image") return "";
    const prefix = `${index + 1}. ${annotationKindLabel(item.kind)}`;
    if (item.kind === "text") return `${prefix}：${text}。保持原位置字体、字重、颜色、透视和光影风格，仅替换对应文字内容。`;
    if (item.kind === "image") return `${prefix}：${item.imageName || "用户上传参考图"}${text ? `，${text}` : ""}`;
    return `${prefix}：${text}`;
  }).filter(Boolean);
  const extra = extraInstruction.trim();
  const multiImageMode = selectedSources.length > 1
    ? "多图模式：当前标注层的坐标只对应画布中显示的主图；其他选中图片请复制同一修改意图，按文字说明、参考图和语义位置进行对应处理，不要把主图坐标强行映射到不同构图的图片。"
    : "";
  return [
    "请基于所选原图进行精准局部修图。箭头尖端、框选区域、圈选区域、文字修改框和上传参考图共同构成本轮修改说明。",
    selectedSources.length > 1 ? `本轮需要一并处理 ${selectedSources.length} 张图片：\n${selectedSources.map((source, index) => `${index + 1}. ${source}`).join("\n")}` : (selectedSources[0] ? `本轮原图：${selectedSources[0]}` : ""),
    multiImageMode,
    "标注附件是透明标注层，细边框表示原图在标注画布中的位置；请把标注层与原图对应起来理解。",
    lines.length ? `标注要求：\n${lines.join("\n")}` : "",
    extra ? `整体要求：${extra}` : "",
    "工具要求：使用 imagegen/图像编辑能力完成语义修图，不要使用 bash、Python、PIL、OpenCV、ImageMagick、SVG/canvas 或坐标脚本直接改图。",
    "保持未标注区域的构图、材质、光照和文字内容稳定，不要额外添加水印或无关元素。"
  ].filter(Boolean).join("\n\n");
}

function wrappedLines(ctx: CanvasRenderingContext2D, text: string, maxWidth: number) {
  const segments = text.replace(/\r/g, "").split("\n");
  const lines: string[] = [];
  for (const segment of segments) {
    let current = "";
    for (const char of segment) {
      const next = current + char;
      if (current && ctx.measureText(next).width > maxWidth) {
        lines.push(current);
        current = char;
      } else {
        current = next;
      }
    }
    lines.push(current || "");
  }
  return lines;
}

function arrowControlPoint(tail: Point, tip: Point) {
  const dx = tip.x - tail.x;
  const dy = tip.y - tail.y;
  const distance = Math.max(1, Math.hypot(dx, dy));
  const bend = clamp(distance * 0.18, 18, 64);
  return {
    x: (tail.x + tip.x) / 2 + (-dy / distance) * bend,
    y: (tail.y + tip.y) / 2 + (dx / distance) * bend
  };
}

function annotationPath(tail: Point, tip: Point) {
  const control = arrowControlPoint(tail, tip);
  return `M ${tail.x} ${tail.y} Q ${control.x} ${control.y} ${tip.x} ${tip.y}`;
}

function stageMetrics(imageSize: { width: number; height: number }) {
  if (!imageSize.width || !imageSize.height) return null;
  const marginX = clamp(imageSize.width * 0.48, 170, 460);
  const marginY = clamp(imageSize.height * 0.3, 120, 320);
  return {
    width: imageSize.width + marginX * 2,
    height: imageSize.height + marginY * 2,
    imageX: marginX,
    imageY: marginY,
    imageWidth: imageSize.width,
    imageHeight: imageSize.height
  };
}

function labelLayout(tail: Point, tip: Point, stageWidth: number, stageHeight: number) {
  const gap = 12;
  let x = tail.x + gap;
  let y = tail.y + 8;
  if (x + TEXT_WIDTH > stageWidth - 8) x = tail.x - TEXT_WIDTH - gap;
  if (x < 8) x = 8;
  if (y + TEXT_HEIGHT > stageHeight - 8) y = tail.y - TEXT_HEIGHT - 8;
  if (y < 8) y = 8;
  if (tail.x < tip.x && x + TEXT_WIDTH > tip.x - gap) {
    x = clamp(tail.x - TEXT_WIDTH - gap, 8, Math.max(8, stageWidth - TEXT_WIDTH - 8));
  }
  return { x, y, width: TEXT_WIDTH, height: TEXT_HEIGHT };
}

function arrowHeadPath(tail: Point, tip: Point, strokeWidth: number) {
  const control = arrowControlPoint(tail, tip);
  const angle = Math.atan2(tip.y - control.y, tip.x - control.x);
  const headLength = clamp(strokeWidth * 4.6, 11, 18);
  const left = {
    x: tip.x - headLength * Math.cos(angle - Math.PI / 6),
    y: tip.y - headLength * Math.sin(angle - Math.PI / 6)
  };
  const right = {
    x: tip.x - headLength * Math.cos(angle + Math.PI / 6),
    y: tip.y - headLength * Math.sin(angle + Math.PI / 6)
  };
  return `M ${left.x} ${left.y} L ${tip.x} ${tip.y} L ${right.x} ${right.y}`;
}

function drawArrow(ctx: CanvasRenderingContext2D, annotation: Annotation) {
  const control = arrowControlPoint(annotation.tail, annotation.tip);
  const head = new Path2D(arrowHeadPath(annotation.tail, annotation.tip, annotation.strokeWidth));
  ctx.save();
  ctx.strokeStyle = annotation.color;
  ctx.lineWidth = annotation.strokeWidth;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  ctx.moveTo(annotation.tail.x, annotation.tail.y);
  ctx.quadraticCurveTo(control.x, control.y, annotation.tip.x, annotation.tip.y);
  ctx.stroke();
  ctx.stroke(head);
  ctx.restore();
}

function drawSelection(ctx: CanvasRenderingContext2D, annotation: Annotation) {
  ctx.save();
  ctx.strokeStyle = annotation.color;
  ctx.lineWidth = annotation.strokeWidth;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.setLineDash(annotation.kind === "lasso" ? [12, 8] : []);
  if (annotation.kind === "rect") {
    const rect = rectFromPoints(annotation.tip, annotation.tail);
    ctx.strokeRect(rect.x, rect.y, rect.width, rect.height);
  } else {
    const path = new Path2D(lassoPath(annotation.points || []));
    ctx.stroke(path);
  }
  ctx.restore();
}

function drawTextTarget(ctx: CanvasRenderingContext2D, annotation: Annotation) {
  const rect = rectFromPoints(annotation.tip, annotation.tail);
  ctx.save();
  ctx.strokeStyle = annotation.color;
  ctx.lineWidth = annotation.strokeWidth;
  ctx.setLineDash([8, 6]);
  ctx.strokeRect(rect.x, rect.y, Math.max(28, rect.width), Math.max(22, rect.height));
  ctx.fillStyle = "rgba(255,255,255,0.84)";
  ctx.fillRect(rect.x, Math.max(0, rect.y - 24), Math.max(54, rect.width), 24);
  ctx.fillStyle = annotation.color;
  ctx.font = "700 18px sans-serif";
  ctx.fillText("T", rect.x + 8, Math.max(18, rect.y - 6));
  ctx.restore();
}

function drawTextLabel(ctx: CanvasRenderingContext2D, annotation: Annotation, width: number, height: number) {
  const layout = labelLayout(annotation.tail, annotation.tip, width, height);
  const maxWidth = layout.width;
  const fontSize = TEXT_SIZE_MAP[annotation.textSize] || TEXT_SIZE_MAP.M;
  ctx.save();
  ctx.font = `600 ${fontSize}px sans-serif`;
  ctx.lineWidth = Math.max(3, fontSize * 0.18);
  ctx.strokeStyle = "rgba(255,255,255,0.92)";
  ctx.fillStyle = annotation.color;
  ctx.lineJoin = "round";
  const lines = wrappedLines(ctx, annotation.text.trim(), maxWidth).slice(0, 6);
  const lineHeight = Math.round(fontSize * 1.35);
  lines.forEach((line, index) => {
    const textY = layout.y + fontSize + index * lineHeight;
    ctx.strokeText(line, layout.x, textY);
    ctx.fillText(line, layout.x, textY);
  });
  ctx.restore();
}

function loadCanvasImage(src: string) {
  return new Promise<HTMLImageElement>((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("参考图加载失败"));
    image.src = src;
  });
}

async function drawUploadedImage(ctx: CanvasRenderingContext2D, annotation: Annotation) {
  if (!annotation.imageUrl) return;
  const rect = rectFromPoints(annotation.tip, annotation.tail);
  const image = await loadCanvasImage(annotation.imageUrl);
  ctx.save();
  ctx.fillStyle = "rgba(255,255,255,0.92)";
  ctx.strokeStyle = annotation.color;
  ctx.lineWidth = annotation.strokeWidth;
  ctx.fillRect(rect.x, rect.y, Math.max(60, rect.width), Math.max(60, rect.height));
  ctx.drawImage(image, rect.x, rect.y, Math.max(60, rect.width), Math.max(60, rect.height));
  ctx.strokeRect(rect.x, rect.y, Math.max(60, rect.width), Math.max(60, rect.height));
  ctx.restore();
}

export function ImageRetouchCanvas({
  title,
  sourcePath,
  imageUrl,
  relatedImages = [],
  busy = false,
  error = "",
  onClose,
  onSubmit
}: ImageRetouchCanvasProps) {
  const imageRef = useRef<HTMLImageElement | null>(null);
  const stageScrollRef = useRef<HTMLDivElement | null>(null);
  const textDraftRef = useRef<HTMLTextAreaElement | null>(null);
  const uploadInputRef = useRef<HTMLInputElement | null>(null);
  const uploadedObjectUrlsRef = useRef<string[]>([]);
  const pointerIdRef = useRef<number | null>(null);
  const panDragRef = useRef<{ pointerId: number; startX: number; startY: number; scrollLeft: number; scrollTop: number } | null>(null);
  const initialCenterDoneRef = useRef(false);
  const [imageSize, setImageSize] = useState({ width: 0, height: 0 });
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [draftAnnotation, setDraftAnnotation] = useState<DraftAnnotation | null>(null);
  const [textDraft, setTextDraft] = useState<TextDraft | null>(null);
  const [extraInstruction, setExtraInstruction] = useState("");
  const [activeTool, setActiveTool] = useState<RetouchTool>("annotate");
  const [annotationColor, setAnnotationColor] = useState(RED);
  const [strokeWidth, setStrokeWidth] = useState(3);
  const [textSize, setTextSize] = useState<AnnotationTextSize>("L");
  const [zoom, setZoom] = useState(1);
  const [localError, setLocalError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const effectiveBusy = busy || submitting;
  const naturalReady = imageSize.width > 0 && imageSize.height > 0;
  const imageChoices = useMemo(() => {
    const choices = relatedImages.length ? relatedImages : [{
      key: sourcePath || imageUrl || "current-image",
      title: title || "当前图片",
      sourcePath,
      previewUrl: imageUrl
    }];
    const seen = new Set<string>();
    return choices.filter((item) => {
      const key = item.key || item.sourcePath || item.previewUrl;
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [imageUrl, relatedImages, sourcePath, title]);
  const [selectedImageKeys, setSelectedImageKeys] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    setSelectedImageKeys((current) => {
      const validKeys = new Set(imageChoices.map((item) => item.key));
      const next = new Set(Array.from(current).filter((key) => validKeys.has(key)));
      if (!next.size && imageChoices[0]) next.add(imageChoices[0].key);
      return next;
    });
  }, [imageChoices]);
  const selectedImages = imageChoices.filter((item) => selectedImageKeys.has(item.key));
  const selectedSources = selectedImages.map((item) => item.sourcePath).filter(Boolean);
  const submitDisabled = effectiveBusy || annotations.length === 0 || !naturalReady || selectedImages.length === 0;
  const metrics = stageMetrics(imageSize);

  useEffect(() => {
    textDraftRef.current?.focus();
  }, [textDraft?.tail.x, textDraft?.tail.y]);

  useEffect(() => {
    if (!metrics || initialCenterDoneRef.current) return;
    initialCenterDoneRef.current = true;
    window.requestAnimationFrame(() => {
      const stage = stageScrollRef.current;
      if (!stage) return;
      stage.scrollLeft = Math.max(0, (stage.scrollWidth - stage.clientWidth) / 2);
      stage.scrollTop = Math.max(0, (stage.scrollHeight - stage.clientHeight) / 2);
    });
  }, [metrics]);

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      const target = event.target instanceof HTMLElement ? event.target : null;
      const editingText = Boolean(textDraft) || target?.tagName === "TEXTAREA" || target?.tagName === "INPUT" || target?.isContentEditable;
      if (event.key === "Escape" && !effectiveBusy && !event.defaultPrevented && !editingText) {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [effectiveBusy, onClose, textDraft]);

  const pointFromPointer = useCallback((event: PointerEvent<SVGSVGElement>): Point | null => {
    const svg = event.currentTarget;
    const rect = svg.getBoundingClientRect();
    if (!rect.width || !rect.height || !metrics) return null;
    return {
      x: clamp((event.clientX - rect.left) * metrics.width / rect.width, 0, metrics.width),
      y: clamp((event.clientY - rect.top) * metrics.height / rect.height, 0, metrics.height)
    };
  }, [metrics]);

  const commitTextDraft = useCallback(() => {
    setTextDraft((current) => {
      const text = current?.text.trim() || "";
      if (!current || !text) return null;
      setAnnotations((items) => [
        ...items,
        {
          id: `${Date.now()}-${items.length + 1}`,
          kind: current.kind,
          tip: current.tip,
          tail: current.tail,
          points: current.points,
          text,
          color: current.color,
          strokeWidth: current.strokeWidth,
          textSize: current.textSize,
          imageUrl: current.imageUrl,
          imageName: current.imageName
        }
      ]);
      return null;
    });
  }, []);

  const cancelTextDraft = useCallback(() => {
    setTextDraft(null);
  }, []);

  const handlePointerDown = (event: PointerEvent<SVGSVGElement>) => {
    if (effectiveBusy || textDraft || activeTool === "hand") return;
    const point = pointFromPointer(event);
    if (!point) return;
    if (activeTool === "text") {
      setLocalError("");
      setTextDraft({
        kind: "text",
        tip: point,
        tail: { x: point.x + 170, y: point.y + 64 },
        text: "",
        color: annotationColor,
        strokeWidth,
        textSize
      });
      return;
    }
    pointerIdRef.current = event.pointerId;
    event.currentTarget.setPointerCapture(event.pointerId);
    setLocalError("");
    setDraftAnnotation({
      kind: activeTool === "rect" ? "rect" : activeTool === "lasso" ? "lasso" : "arrow",
      tip: point,
      tail: point,
      points: activeTool === "lasso" ? [point] : undefined
    });
  };

  const handlePointerMove = (event: PointerEvent<SVGSVGElement>) => {
    if (pointerIdRef.current !== event.pointerId) return;
    const point = pointFromPointer(event);
    if (!point) return;
    setDraftAnnotation((current) => {
      if (!current) return current;
      if (current.kind === "lasso") {
        const points = current.points || [];
        const last = points[points.length - 1];
        if (last && pointDistance(last, point) < 4) return current;
        const nextPoints = [...points, point];
        const bounds = boundsFromPoints(nextPoints);
        return {
          ...current,
          points: nextPoints,
          tip: { x: bounds.x, y: bounds.y },
          tail: { x: bounds.x + bounds.width, y: bounds.y + bounds.height }
        };
      }
      return { ...current, tail: point };
    });
  };

  const finishPointer = (event: PointerEvent<SVGSVGElement>) => {
    if (pointerIdRef.current !== event.pointerId) return;
    pointerIdRef.current = null;
    try {
      event.currentTarget.releasePointerCapture(event.pointerId);
    } catch {
      // Pointer capture can already be released if the pointer leaves the window.
    }
    setDraftAnnotation((current) => {
      const enough = current?.kind === "lasso"
        ? (current.points || []).length >= 4 && rectFromPoints(current.tip, current.tail).width > MIN_ARROW_DISTANCE && rectFromPoints(current.tip, current.tail).height > MIN_ARROW_DISTANCE
        : current && pointDistance(current.tip, current.tail) >= MIN_ARROW_DISTANCE;
      if (!current || !enough) return null;
      setTextDraft({ ...current, text: "", color: annotationColor, strokeWidth, textSize });
      return null;
    });
  };

  const handleTextDraftKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      commitTextDraft();
    }
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      cancelTextDraft();
    }
  };

  const setClampedZoom = useCallback((value: number | ((current: number) => number)) => {
    setZoom((current) => {
      const next = typeof value === "function" ? value(current) : value;
      return Math.round(clamp(next, MIN_ZOOM, MAX_ZOOM) * 100) / 100;
    });
  }, []);

  const centerStage = useCallback(() => {
    const stage = stageScrollRef.current;
    if (!stage) return;
    stage.scrollLeft = Math.max(0, (stage.scrollWidth - stage.clientWidth) / 2);
    stage.scrollTop = Math.max(0, (stage.scrollHeight - stage.clientHeight) / 2);
  }, []);

  const resetZoom = useCallback(() => {
    setClampedZoom(1);
    window.requestAnimationFrame(centerStage);
  }, [centerStage, setClampedZoom]);

  const handleUploadReferenceImage = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file || !metrics) return;
    if (!/^image\//i.test(file.type)) {
      setLocalError("请选择图片文件");
      return;
    }
    const objectUrl = URL.createObjectURL(file);
    uploadedObjectUrlsRef.current.push(objectUrl);
    const width = clamp(metrics.imageWidth * 0.22, 120, 260);
    const height = clamp(width * 0.72, 90, 190);
    const tail = {
      x: clamp(metrics.imageX + metrics.imageWidth + 26, 8, metrics.width - width - 8),
      y: clamp(metrics.imageY + annotations.length * 22, 8, metrics.height - height - 8)
    };
    setAnnotations((items) => [
      ...items,
      {
        id: `${Date.now()}-${items.length + 1}-image`,
        kind: "image",
        tip: tail,
        tail: { x: tail.x + width, y: tail.y + height },
        text: "作为局部风格、元素或替换参考",
        color: annotationColor,
        strokeWidth,
        textSize,
        imageUrl: objectUrl,
        imageName: file.name
      }
    ]);
  }, [annotationColor, annotations.length, metrics, strokeWidth, textSize]);

  useEffect(() => () => {
    uploadedObjectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    uploadedObjectUrlsRef.current = [];
  }, []);

  const handleStageWheel = useCallback((event: WheelEvent<HTMLDivElement>) => {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    const direction = event.deltaY > 0 ? -1 : 1;
    setClampedZoom((current) => current + direction * ZOOM_STEP);
  }, [setClampedZoom]);

  const handleStagePointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (activeTool !== "hand" || event.button !== 0) return;
    const stage = stageScrollRef.current;
    if (!stage) return;
    event.preventDefault();
    panDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      scrollLeft: stage.scrollLeft,
      scrollTop: stage.scrollTop
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const handleStagePointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const drag = panDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const stage = stageScrollRef.current;
    if (!stage) return;
    stage.scrollLeft = drag.scrollLeft - (event.clientX - drag.startX);
    stage.scrollTop = drag.scrollTop - (event.clientY - drag.startY);
  };

  const finishStagePointer = (event: PointerEvent<HTMLDivElement>) => {
    const drag = panDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    panDragRef.current = null;
    try {
      event.currentTarget.releasePointerCapture(event.pointerId);
    } catch {
      // Pointer capture can already be released when the window loses focus.
    }
  };

  const exportAnnotationLayer = useCallback(async () => {
    const image = imageRef.current;
    if (!image || !image.naturalWidth || !image.naturalHeight) {
      throw new Error("图片尚未加载完成");
    }
    const canvas = document.createElement("canvas");
    const exportMetrics = stageMetrics({ width: image.naturalWidth, height: image.naturalHeight });
    if (!exportMetrics) throw new Error("图片尚未加载完成");
    canvas.width = Math.round(exportMetrics.width);
    canvas.height = Math.round(exportMetrics.height);
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("当前浏览器不支持图像导出");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.save();
    ctx.strokeStyle = "rgba(17, 24, 39, 0.45)";
    ctx.lineWidth = 2;
    ctx.setLineDash([10, 8]);
    ctx.strokeRect(exportMetrics.imageX, exportMetrics.imageY, exportMetrics.imageWidth, exportMetrics.imageHeight);
    ctx.restore();
    for (const annotation of annotations) {
      if (annotation.kind === "arrow") {
        drawArrow(ctx, annotation);
      } else if (annotation.kind === "rect" || annotation.kind === "lasso") {
        drawSelection(ctx, annotation);
      } else if (annotation.kind === "text") {
        drawTextTarget(ctx, annotation);
      } else if (annotation.kind === "image") {
        await drawUploadedImage(ctx, annotation);
      }
      if (annotation.text) {
        drawTextLabel(ctx, annotation, canvas.width, canvas.height);
      }
    }
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!blob) throw new Error("标注图导出失败");
    return blob;
  }, [annotations]);

  const handleSubmit = async () => {
    if (submitDisabled) return;
    setSubmitting(true);
    setLocalError("");
    try {
      const annotatedBlob = await exportAnnotationLayer();
      await onSubmit({
        annotatedBlob,
        fileName: `retouch-marker-layer-${Date.now()}.png`,
        prompt: annotationPrompt(annotations, extraInstruction, selectedSources),
        annotationCount: annotations.length,
        selectedSources,
        selectedCount: selectedSources.length,
        textEditCount: annotations.filter((annotation) => annotation.kind === "text").length
      });
    } catch (submitError) {
      setLocalError(submitError instanceof Error ? submitError.message : "加入聊天框失败");
    } finally {
      setSubmitting(false);
    }
  };

  const viewBox = metrics ? `0 0 ${metrics.width} ${metrics.height}` : undefined;
  const imagePlacementStyle = metrics ? {
    left: `${(metrics.imageX / metrics.width) * 100}%`,
    top: `${(metrics.imageY / metrics.height) * 100}%`,
    width: `${(metrics.imageWidth / metrics.width) * 100}%`,
    height: `${(metrics.imageHeight / metrics.height) * 100}%`
  } as CSSProperties : undefined;
  const stageStyle = metrics ? {
    "--retouch-stage-aspect": `${metrics.width / metrics.height}`,
    "--retouch-stage-zoom": `${zoom}`,
    aspectRatio: `${metrics.width} / ${metrics.height}`
  } as CSSProperties : undefined;
  const zoomPercent = Math.round(zoom * 100);
  const textDraftRect = textDraft && metrics ? labelLayout(textDraft.tail, textDraft.tip, metrics.width, metrics.height) : null;
  const textDraftStyle = textDraft && metrics && textDraftRect ? {
    left: `${(textDraftRect.x / metrics.width) * 100}%`,
    top: `${(textDraftRect.y / metrics.height) * 100}%`,
    width: `${(textDraftRect.width / metrics.width) * 100}%`,
    "--retouch-accent": textDraft.color,
    "--retouch-text-size": `${TEXT_SIZE_MAP[textDraft.textSize] || TEXT_SIZE_MAP.M}px`
  } as CSSProperties : undefined;
  const visibleAnnotations: Annotation[] = [
    ...annotations,
    ...(draftAnnotation ? [{
      id: "draft",
      kind: draftAnnotation.kind,
      tip: draftAnnotation.tip,
      tail: draftAnnotation.tail,
      points: draftAnnotation.points,
      text: "",
      color: annotationColor,
      strokeWidth,
      textSize
    }] : [])
  ];

  return (
    <div className="modal-backdrop image-retouch-backdrop" role="dialog" aria-modal="true" aria-label="精准修图">
      <section className="image-retouch-sheet is-editor">
        <header className="image-retouch-header">
          <div className="image-retouch-titlebar">
            <button type="button" className="image-retouch-top-icon" title="菜单" aria-label="菜单">
              <Menu aria-hidden="true" />
            </button>
            <strong title={title || "图片精准修图"}>{title || "图片精准修图"}</strong>
            <button type="button" className="image-retouch-top-icon" title={sourcePath || "图片来源"} aria-label="图片来源">
              <ChevronDown aria-hidden="true" />
            </button>
            <span className="image-retouch-divider" />
            <button type="button" className="image-retouch-top-icon" title="撤销上一处标注" onClick={() => setAnnotations((items) => items.slice(0, -1))} disabled={effectiveBusy || annotations.length === 0}>
              <RotateCcw aria-hidden="true" />
            </button>
            <button type="button" className="image-retouch-top-icon" title="清空全部标注" onClick={() => setAnnotations([])} disabled={effectiveBusy || annotations.length === 0}>
              <Trash2 aria-hidden="true" />
            </button>
            <button type="button" className="image-retouch-top-icon" title="复制标注数量" aria-label="复制标注数量" onClick={() => void navigator.clipboard?.writeText(`${annotations.length} 处标注`)}>
              <Copy aria-hidden="true" />
            </button>
            <button type="button" className="image-retouch-top-icon" title="更多" aria-label="更多">
              <MoreVertical aria-hidden="true" />
            </button>
          </div>
          <div className="image-retouch-header-actions">
            <span className="image-retouch-count">{selectedImages.length} 张图 / {annotations.length} 处标注{selectedImages.length > 1 ? " · 复制同一修改意图" : ""}</span>
            <button type="button" className="image-retouch-submit-mini" onClick={() => void handleSubmit()} disabled={submitDisabled}>
              <SendHorizontal aria-hidden="true" />{effectiveBusy ? "加入中" : "加入聊天框"}
            </button>
            <button type="button" className="icon-button" title="关闭" aria-label="关闭精准修图" onClick={onClose} disabled={effectiveBusy}>
              <X aria-hidden="true" />
            </button>
          </div>
        </header>

        <div className="image-retouch-body is-editor">
          <div
            ref={stageScrollRef}
            className={`image-retouch-stage${activeTool === "hand" ? " is-hand" : ""}`}
            onWheel={handleStageWheel}
            onPointerDown={handleStagePointerDown}
            onPointerMove={handleStagePointerMove}
            onPointerUp={finishStagePointer}
            onPointerCancel={finishStagePointer}
          >
            <div className="image-retouch-stage-inner">
              <div className={`image-retouch-image-wrap${metrics ? " has-stage" : ""}`} style={stageStyle}>
              <img
                ref={imageRef}
                src={imageUrl}
                alt={title || "待修图图片"}
                draggable={false}
                style={imagePlacementStyle}
                onLoad={(event) => {
                  setImageSize({
                    width: event.currentTarget.naturalWidth,
                    height: event.currentTarget.naturalHeight
                  });
                }}
                onError={() => setLocalError("图片预览加载失败")}
              />
              {metrics && viewBox ? (
                <svg
                  className="image-retouch-overlay"
                  viewBox={viewBox}
                  onPointerDown={handlePointerDown}
                  onPointerMove={handlePointerMove}
                  onPointerUp={finishPointer}
                  onPointerCancel={finishPointer}
                >
                  {visibleAnnotations.map((annotation) => (
                    <g key={annotation.id} className={annotation.id === "draft" ? "is-draft" : undefined}>
                      {annotation.kind === "arrow" ? (
                        <>
                          <path
                            d={annotationPath(annotation.tail, annotation.tip)}
                            style={{ stroke: annotation.color, strokeWidth: annotation.strokeWidth }}
                          />
                          <path
                            d={arrowHeadPath(annotation.tail, annotation.tip, annotation.strokeWidth)}
                            style={{ stroke: annotation.color, strokeWidth: annotation.strokeWidth }}
                          />
                        </>
                      ) : annotation.kind === "rect" ? (
                        <rect
                          {...rectFromPoints(annotation.tip, annotation.tail)}
                          className="image-retouch-selection-shape"
                          style={{ stroke: annotation.color, strokeWidth: annotation.strokeWidth }}
                        />
                      ) : annotation.kind === "lasso" ? (
                        <path
                          d={lassoPath(annotation.points || [])}
                          className="image-retouch-selection-shape is-lasso"
                          style={{ stroke: annotation.color, strokeWidth: annotation.strokeWidth }}
                        />
                      ) : annotation.kind === "text" ? (
                        <rect
                          {...rectFromPoints(annotation.tip, annotation.tail)}
                          className="image-retouch-text-target-shape"
                          style={{ stroke: annotation.color, strokeWidth: annotation.strokeWidth }}
                        />
                      ) : null}
                      {annotation.text ? (
                        <foreignObject
                          {...labelLayout(annotation.tail, annotation.tip, metrics.width, metrics.height)}
                        >
                          <div
                            className="image-retouch-svg-label"
                            style={{ color: annotation.color, fontSize: TEXT_SIZE_MAP[annotation.textSize] || TEXT_SIZE_MAP.M }}
                          >
                            {annotation.text}
                          </div>
                        </foreignObject>
                      ) : null}
                    </g>
                  ))}
                </svg>
              ) : null}
              {metrics ? (
                <div className="image-retouch-sticker-layer" aria-hidden="true">
                  {annotations.filter((annotation) => annotation.kind === "image" && annotation.imageUrl).map((annotation) => {
                    const rect = rectFromPoints(annotation.tip, annotation.tail);
                    return (
                      <img
                        key={annotation.id}
                        src={annotation.imageUrl}
                        alt=""
                        style={{
                          left: `${(rect.x / metrics.width) * 100}%`,
                          top: `${(rect.y / metrics.height) * 100}%`,
                          width: `${(Math.max(60, rect.width) / metrics.width) * 100}%`,
                          height: `${(Math.max(60, rect.height) / metrics.height) * 100}%`,
                          borderColor: annotation.color
                        }}
                      />
                    );
                  })}
                </div>
              ) : null}
              {textDraft && textDraftStyle ? (
                <div className="image-retouch-text-draft" style={textDraftStyle}>
                  <textarea
                    ref={textDraftRef}
                    value={textDraft.text}
                    rows={3}
                    placeholder="修改说明"
                    onChange={(event) => setTextDraft((current) => current ? { ...current, text: event.target.value } : current)}
                    onKeyDown={handleTextDraftKeyDown}
                  />
                  <div>
                    <button type="button" title="完成标注" aria-label="完成标注" onClick={commitTextDraft}>
                      <CornerDownLeft aria-hidden="true" />
                    </button>
                    <button type="button" title="取消标注" aria-label="取消标注" onClick={cancelTextDraft}>
                      <X aria-hidden="true" />
                    </button>
                  </div>
                </div>
              ) : null}
              </div>
            </div>
          </div>

          <aside className="image-retouch-style-panel" aria-label="标注样式">
            <div className="image-retouch-color-grid">
              {COLOR_SWATCHES.map((color) => (
                <button
                  key={color}
                  type="button"
                  className={annotationColor === color ? "is-active" : ""}
                  style={{ background: color }}
                  onClick={() => setAnnotationColor(color)}
                  title={`颜色 ${color}`}
                  aria-label={`颜色 ${color}`}
                />
              ))}
            </div>
            <input
              type="range"
              min="2"
              max="6"
              step="0.5"
              value={strokeWidth}
              onChange={(event) => setStrokeWidth(Number(event.currentTarget.value))}
              aria-label="箭头粗细"
            />
            <div className="image-retouch-size-row" aria-label="文字大小">
              {(["S", "M", "L", "XL"] as AnnotationTextSize[]).map((size) => (
                <button key={size} type="button" className={textSize === size ? "is-active" : ""} onClick={() => setTextSize(size)}>
                  {size}
                </button>
              ))}
            </div>
            <textarea
              className="image-retouch-extra-inline"
              value={extraInstruction}
              rows={3}
              placeholder="整体要求"
              onChange={(event) => setExtraInstruction(event.target.value)}
              disabled={effectiveBusy}
            />
          </aside>

          <aside className="image-retouch-side-panel" aria-label="本轮图片">
            <div className="image-retouch-side-head">
              <strong>本轮产物</strong>
              <span>{selectedImages.length}/{imageChoices.length}{selectedImages.length > 1 ? " 复制意图" : ""}</span>
            </div>
            <div className="image-retouch-image-picker">
              {imageChoices.map((item) => {
                const checked = selectedImageKeys.has(item.key);
                return (
                  <button
                    key={item.key}
                    type="button"
                    className={checked ? "is-selected" : ""}
                    onClick={() => {
                      setSelectedImageKeys((current) => {
                        const next = new Set(current);
                        if (next.has(item.key) && next.size > 1) {
                          next.delete(item.key);
                        } else {
                          next.add(item.key);
                        }
                        return next;
                      });
                    }}
                    title={item.sourcePath || item.title}
                  >
                    {item.previewUrl ? <img src={item.previewUrl} alt="" loading="lazy" /> : <ImageIcon aria-hidden="true" />}
                    <span>{item.title || "图片"}</span>
                  </button>
                );
              })}
            </div>
          </aside>

          <div className="image-retouch-bottom-toolbar" aria-label="精准修图工具">
            <button type="button" className="is-label" title="标注工具">
              <PenLine aria-hidden="true" />
              标注
            </button>
            <button type="button" className={activeTool === "annotate" ? "is-active" : ""} onClick={() => setActiveTool("annotate")} title="箭头标注">
              <MousePointer2 aria-hidden="true" />
            </button>
            <button type="button" className={activeTool === "rect" ? "is-active" : ""} onClick={() => setActiveTool("rect")} title="框选">
              <Square aria-hidden="true" />
            </button>
            <button type="button" className={activeTool === "lasso" ? "is-active" : ""} onClick={() => setActiveTool("lasso")} title="圈选">
              <Circle aria-hidden="true" />
            </button>
            <button type="button" className={activeTool === "text" ? "is-active" : ""} onClick={() => setActiveTool("text")} title="文字修改">
              <Type aria-hidden="true" />
            </button>
            <button type="button" className={activeTool === "hand" ? "is-active" : ""} onClick={() => setActiveTool("hand")} title="浏览">
              <Hand aria-hidden="true" />
            </button>
            <button type="button" title="缩小" aria-label="缩小画布" onClick={() => setClampedZoom((current) => current - ZOOM_STEP)}>
              <ZoomOut aria-hidden="true" />
            </button>
            <button type="button" title="适配画布" aria-label="适配画布" onClick={resetZoom}>
              <Maximize aria-hidden="true" />
            </button>
            <button type="button" title="放大" aria-label="放大画布" onClick={() => setClampedZoom((current) => current + ZOOM_STEP)}>
              <ZoomIn aria-hidden="true" />
            </button>
            <button type="button" title="上传参考图" aria-label="上传参考图" onClick={() => uploadInputRef.current?.click()} disabled={!metrics || effectiveBusy}>
              <Upload aria-hidden="true" />
            </button>
            <button type="button" className={activeTool === "annotate" ? "is-active-secondary" : ""} onClick={() => setActiveTool("annotate")} title="画笔">
              <PenLine aria-hidden="true" />
            </button>
            <button type="button" onClick={() => setAnnotations((items) => items.slice(0, -1))} disabled={effectiveBusy || annotations.length === 0} title="擦除上一处">
              <Eraser aria-hidden="true" />
            </button>
          </div>
          <input ref={uploadInputRef} className="image-retouch-hidden-input" type="file" accept="image/*" onChange={handleUploadReferenceImage} />

          <div className="image-retouch-zoom-chip">{zoomPercent}%</div>
          {(localError || error) ? <div className="image-retouch-error is-floating">{localError || error}</div> : null}
        </div>
      </section>
    </div>
  );
}
