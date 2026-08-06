import type {
  InteractionProjection,
  ItemProjection,
  TurnProjection,
} from "../api/contracts.ts";

const TERMINAL = new Set<TurnProjection["status"]>([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
  "superseded",
]);

export type TimelineBlock =
  | { kind: "item"; key: string; item: ItemProjection }
  | { kind: "interaction"; key: string; interaction: InteractionProjection };

export interface TimelineTurn {
  turn: TurnProjection;
  blocks: TimelineBlock[];
  userBlocks: TimelineBlock[];
  assistantBlocks: TimelineBlock[];
  terminal: boolean;
}

export interface TimelineSegment {
  segmentId: string;
  kind: "anchor" | "process";
  blocks: TimelineBlock[];
  defaultOpen: boolean;
}

function isUserBlock(block: TimelineBlock): boolean {
  return block.kind === "item"
    && block.item.kind === "message"
    && block.item.content.role === "user";
}

function messageText(block: TimelineBlock): string {
  if (block.kind !== "item" || block.item.kind !== "message") return "";
  const value = block.item.content.text ?? block.item.content.content;
  return typeof value === "string" ? value.trim() : "";
}

function sequence(block: TimelineBlock): number | null | undefined {
  return block.kind === "item"
    ? block.item.created_seq
    : block.interaction.created_seq;
}

function isPresent(block: TimelineBlock): boolean {
  return block.kind !== "item"
    || block.item.kind !== "reasoning"
    || block.item.content.presentation === "visible"
    || block.item.content.presentation === "collapsed";
}

export function buildTimelineTurns(
  turns: readonly TurnProjection[],
  items: readonly ItemProjection[],
  interactions: readonly InteractionProjection[],
): TimelineTurn[] {
  const byTurn = new Map<string, Array<{ block: TimelineBlock; fallback: number }>>();
  const push = (turnId: string | null, block: TimelineBlock, fallback: number) => {
    if (!turnId) return;
    const blocks = byTurn.get(turnId) ?? [];
    blocks.push({ block, fallback });
    byTurn.set(turnId, blocks);
  };
  items.forEach((item, index) => push(
    item.turn_id,
    { kind: "item", key: item.item_id, item },
    index,
  ));
  interactions.forEach((interaction, index) => push(
    interaction.turn_id,
    { kind: "interaction", key: interaction.interaction_id, interaction },
    items.length + index,
  ));

  return turns.map((turn) => {
    const blocks = (byTurn.get(turn.turn_id) ?? [])
      .sort((left, right) => {
        const leftSeq = sequence(left.block);
        const rightSeq = sequence(right.block);
        if (leftSeq != null && rightSeq != null) return leftSeq - rightSeq;
        if (leftSeq != null) return -1;
        if (rightSeq != null) return 1;
        return left.fallback - right.fallback;
      })
      .map(({ block }) => block)
      .filter(isPresent);
    const userBlocks = blocks.filter(isUserBlock);
    return {
      turn,
      blocks,
      userBlocks,
      assistantBlocks: blocks.filter((block) => !isUserBlock(block)),
      terminal: TERMINAL.has(turn.status),
    };
  });
}

export function foldTurnProcess(entry: TimelineTurn): TimelineSegment[] {
  if (!entry.terminal) {
    return entry.blocks.map((block) => ({
      segmentId: `live-${block.key}`,
      kind: "anchor",
      blocks: [block],
      defaultOpen: true,
    }));
  }

  const assistantMessages = entry.blocks.filter((block) => (
    !isUserBlock(block) && messageText(block).length > 0
  ));
  const longest = assistantMessages.reduce<TimelineBlock | null>((selected, block) => (
    !selected || messageText(block).replace(/\s+/gu, "").length
      > messageText(selected).replace(/\s+/gu, "").length
      ? block
      : selected
  ), null);
  const finalMessage = assistantMessages.at(-1) ?? null;
  const anchors = new Set(
    [longest, finalMessage]
      .filter((block): block is TimelineBlock => block !== null)
      .map((block) => block.key),
  );
  for (const block of entry.blocks) {
    if (
      isUserBlock(block)
      || (block.kind === "item" && block.item.kind === "artifact")
      || (block.kind === "interaction" && block.interaction.status === "pending")
    ) anchors.add(block.key);
  }

  const segments: TimelineSegment[] = [];
  let pending: TimelineBlock[] = [];
  let leftAnchor: string | null = null;
  const anchorOrder = entry.blocks.filter((block) => anchors.has(block.key));
  let anchorIndex = 0;
  const flush = () => {
    if (!pending.length) return;
    const rightAnchor = anchorOrder[anchorIndex]?.key ?? null;
    const segmentId = leftAnchor === null
      ? "completion-process"
      : rightAnchor === null
      ? "tail-process"
      : `process-${leftAnchor}-${rightAnchor}`;
    const failureWithoutAnswer = assistantMessages.length === 0
      && entry.turn.status !== "completed"
      && entry.turn.status !== "superseded";
    segments.push({
      segmentId,
      kind: "process",
      blocks: pending,
      defaultOpen: failureWithoutAnswer && !segments.some((segment) => segment.kind === "process"),
    });
    pending = [];
  };

  for (const block of entry.blocks) {
    if (!anchors.has(block.key)) {
      pending.push(block);
      continue;
    }
    flush();
    segments.push({
      segmentId: `anchor-${block.key}`,
      kind: "anchor",
      blocks: [block],
      defaultOpen: true,
    });
    leftAnchor = block.key;
    anchorIndex += 1;
  }
  flush();
  return segments;
}
