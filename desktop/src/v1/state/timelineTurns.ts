import type {
  InteractionProjection,
  ItemProjection,
  TurnProjection,
} from "../api/contracts.ts";

const TERMINAL = new Set<TurnProjection["status"]>([
  "completed",
  "partial",
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

function isUserBlock(block: TimelineBlock): boolean {
  return block.kind === "item"
    && block.item.kind === "message"
    && block.item.content.role === "user";
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
