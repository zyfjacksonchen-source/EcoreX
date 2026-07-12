export const TIMELINE_WINDOW_SIZE = 120;

export interface TimelineWindowItem {
  item_id: string;
}

export interface TimelineWindow<T extends TimelineWindowItem> {
  items: T[];
  startIndex: number;
  endIndex: number;
  hiddenBefore: number;
  hiddenAfter: number;
  atLatest: boolean;
  anchorMissing: boolean;
}

export function selectTimelineWindow<T extends TimelineWindowItem>(
  items: readonly T[],
  endAnchorId: string | null,
  windowSize = TIMELINE_WINDOW_SIZE,
): TimelineWindow<T> {
  if (!Number.isSafeInteger(windowSize) || windowSize < 1) {
    throw new TypeError("Timeline window size must be a positive integer");
  }
  const anchorIndex = endAnchorId === null
    ? -1
    : items.findIndex((item) => item.item_id === endAnchorId);
  const anchorMissing = endAnchorId !== null && anchorIndex < 0;
  const endIndex = anchorMissing || endAnchorId === null
    ? items.length
    : anchorIndex + 1;
  const startIndex = Math.max(0, endIndex - windowSize);
  return {
    items: items.slice(startIndex, endIndex),
    startIndex,
    endIndex,
    hiddenBefore: startIndex,
    hiddenAfter: items.length - endIndex,
    atLatest: endIndex === items.length,
    anchorMissing,
  };
}

export function earlierTimelineAnchor<T extends TimelineWindowItem>(
  items: readonly T[],
  window: TimelineWindow<T>,
): string | null {
  if (window.startIndex === 0) return null;
  return items[window.startIndex - 1]?.item_id ?? null;
}

export function newerTimelineAnchor<T extends TimelineWindowItem>(
  items: readonly T[],
  window: TimelineWindow<T>,
  windowSize = TIMELINE_WINDOW_SIZE,
): string | null {
  const nextEnd = Math.min(items.length, window.endIndex + windowSize);
  return nextEnd === items.length ? null : items[nextEnd - 1]?.item_id ?? null;
}
