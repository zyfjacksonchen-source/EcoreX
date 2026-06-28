import type { RuntimeMessage, RuntimeRequestProjection } from "../services/ecorexApi";

export type ProjectionTerminalPhase = "completed" | "failed" | "cancelled" | "interrupted";

export type ProjectionRecoveryDecision =
  | {
      handled: false;
      reason: "empty" | "no-assistant" | "non-terminal";
    }
  | {
      handled: true;
      terminalPhase: ProjectionTerminalPhase;
      content: string;
      cancelled: boolean;
      markCompleted: boolean;
      messages: RuntimeMessage[];
    };

export function projectionTerminalPhase(state?: string): ProjectionTerminalPhase | "" {
  if (state === "completed") return "completed";
  if (state === "failed") return "failed";
  if (state === "cancelled") return "cancelled";
  if (state === "interrupted") return "interrupted";
  return "";
}

export function projectionRecoveryDecision(projection?: RuntimeRequestProjection | null): ProjectionRecoveryDecision {
  if (!projection || !projection.event_count) {
    return { handled: false, reason: "empty" };
  }
  const messages = Array.isArray(projection.messages) ? projection.messages : [];
  const assistant = messages.find((message) => message.role === "assistant");
  if (!assistant) {
    return { handled: false, reason: "no-assistant" };
  }
  const terminalPhase = projectionTerminalPhase(projection.state);
  if (!terminalPhase) {
    return { handled: false, reason: "non-terminal" };
  }
  const terminalMessage = String(projection.terminal_message || "");
  const assistantContent = String(assistant.content || "");
  const content = terminalPhase === "completed"
    ? (assistantContent || terminalMessage)
    : (terminalMessage || assistantContent);
  return {
    handled: true,
    terminalPhase,
    content,
    cancelled: terminalPhase === "cancelled",
    markCompleted: terminalPhase === "completed",
    messages
  };
}
