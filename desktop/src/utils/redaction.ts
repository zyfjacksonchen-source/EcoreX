function markerFromCodes(codes: number[]) {
  return String.fromCharCode(...codes);
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const EXTERNAL_CAPABILITY_CHAIN_MARKER = markerFromCodes([
  89, 111, 117, 32, 104, 97, 118, 101, 32, 117, 115, 101, 100, 32, 116, 104,
  101, 32, 115, 97, 109, 101, 32, 101, 120, 116, 101, 114, 110, 97, 108, 32,
  99, 97, 112, 97, 98, 105, 108, 105, 116, 121, 32, 99, 104, 97, 105, 110
]);
const INTERNAL_PROMPT_MARKERS = [
  EXTERNAL_CAPABILITY_CHAIN_MARKER,
  markerFromCodes([
    73, 102, 32, 101, 110, 111, 117, 103, 104, 32, 105, 110, 102, 111, 114,
    109, 97, 116, 105, 111, 110, 32, 104, 97, 115, 32, 98, 101, 101, 110, 32,
    99, 111, 108, 108, 101, 99, 116, 101, 100, 44, 32, 112, 114, 111, 118,
    105, 100, 101, 32, 116, 104, 101, 32, 102, 105, 110, 97, 108, 32, 97,
    110, 115, 119, 101, 114, 32, 110, 111, 119
  ]),
  markerFromCodes([
    68, 111, 32, 110, 111, 116, 32, 99, 111, 110, 116, 105, 110, 117, 101,
    32, 112, 114, 111, 98, 105, 110, 103, 32, 116, 104, 101, 32, 115, 97,
    109, 101, 32, 99, 104, 97, 105, 110, 32, 117, 110, 108, 101, 115, 115,
    32, 116, 104, 101, 32, 110, 101, 120, 116, 32, 99, 97, 108, 108, 32,
    105, 115, 32, 99, 108, 101, 97, 114, 108, 121, 32, 110, 101, 119, 32,
    97, 110, 100, 32, 110, 101, 99, 101, 115, 115, 97, 114, 121
  ]),
  markerFromCodes([
    70, 101, 105, 115, 104, 117, 47, 76, 97, 114, 107, 32, 116, 111, 111,
    108, 32, 99, 104, 97, 105, 110, 32, 104, 97, 115, 32, 98, 101, 101, 110,
    32, 117, 115, 101, 100, 32, 114, 101, 112, 101, 97, 116, 101, 100, 108,
    121, 32, 119, 105, 116, 104, 111, 117, 116, 32, 99, 111, 110, 118, 101,
    114, 103, 105, 110, 103
  ]),
  markerFromCodes([
    66, 114, 111, 119, 115, 101, 114, 47, 67, 68, 80, 32, 116, 111, 111,
    108, 32, 99, 104, 97, 105, 110, 32, 104, 97, 115, 32, 98, 101, 101, 110,
    32, 117, 115, 101, 100, 32, 114, 101, 112, 101, 97, 116, 101, 100, 108,
    121, 32, 119, 105, 116, 104, 111, 117, 116, 32, 99, 111, 110, 118, 101,
    114, 103, 105, 110, 103
  ]),
  markerFromCodes([
    83, 104, 101, 108, 108, 32, 116, 111, 111, 108, 32, 99, 104, 97, 105,
    110, 32, 104, 97, 115, 32, 98, 101, 101, 110, 32, 117, 115, 101, 100,
    32, 114, 101, 112, 101, 97, 116, 101, 100, 108, 121, 32, 119, 105, 116,
    104, 111, 117, 116, 32, 99, 111, 110, 118, 101, 114, 103, 105, 110, 103
  ]),
  markerFromCodes([
    72, 111, 115, 116, 32, 99, 97, 112, 97, 98, 105, 108, 105, 116, 121,
    32, 98, 111, 117, 110, 100, 97, 114, 121, 58
  ])
];
const EXTERNAL_CAPABILITY_CHAIN_PATTERN = new RegExp(`${escapeRegExp(EXTERNAL_CAPABILITY_CHAIN_MARKER)}[\\s\\S]*?(?:necessary\\.|$)`, "g");
const TOOL_CHAIN_REPEAT_TAIL = markerFromCodes([
  32, 116, 111, 111, 108, 32, 99, 104, 97, 105, 110, 32, 104, 97, 115, 32,
  98, 101, 101, 110, 32, 117, 115, 101, 100, 32, 114, 101, 112, 101, 97,
  116, 101, 100, 108, 121, 32, 119, 105, 116, 104, 111, 117, 116, 32, 99,
  111, 110, 118, 101, 114, 103, 105, 110, 103
]);
const TOOL_CHAIN_PROVIDER_PATTERN = markerFromCodes([
  40, 63, 58, 70, 101, 105, 115, 104, 117, 92, 47, 76, 97, 114, 107, 124,
  66, 114, 111, 119, 115, 101, 114, 92, 47, 67, 68, 80, 124, 83, 104, 101,
  108, 108, 41
]);
const TOOL_CHAIN_END_PATTERN = markerFromCodes([
  105, 110, 112, 117, 116, 32, 105, 102, 32, 110, 101, 101, 100, 101, 100,
  92, 46, 124, 99, 111, 110, 102, 105, 114, 109, 97, 116, 105, 111, 110, 92,
  46, 124, 36
]);
const TOOL_CHAIN_REPEAT_PATTERN = new RegExp(`${TOOL_CHAIN_PROVIDER_PATTERN}${escapeRegExp(TOOL_CHAIN_REPEAT_TAIL)}[\\s\\S]*?(?:${TOOL_CHAIN_END_PATTERN})`, "g");

const USER_VISIBLE_REPLACEMENT = "已停止重复调用同一能力，正在整理已获得的信息。";
const NETWORK_ERROR_REPLACEMENT = "网络连接被中断，请稍后重试；如果持续出现，请检查当前网络、代理或模型接口地址。";
const RAW_NETWORK_ERROR_PATTERN = /(?:❌\s*)?(?:Connection error|Stream interrupted|Stream error):[\s\S]*?(?:ConnectionResetError\([^)]*\)|connection reset by peer|Connection aborted\.[\s\S]*?)(?:\s*\(Status:\s*0,\s*Code:\s*,\s*Type:\s*\))?/gi;
const RAW_STATUS_ZERO_PATTERN = /[\s\S]*?(?:ConnectionResetError|connection reset by peer|Connection aborted)[\s\S]*?\(Status:\s*0,\s*Code:\s*,\s*Type:\s*\)[\s\S]*/i;
const TOOL_SENSITIVE_KEY_PATTERN = /(api[_-]?key|token|password|passwd|secret|authorization|cookie|session)/i;
const TOOL_CONTENT_KEY_PATTERN = /^(content|contents|body|file_content|file_contents|source|script|code|markdown|prompt|instructions?)$/i;
const TOOL_SENSITIVE_TEXT_PATTERNS: Array<[RegExp, string]> = [
  [/sk-[A-Za-z0-9_\-]{8,}/g, "sk-***"],
  [/gh[pousr]_[A-Za-z0-9_]{8,}/g, "ghp_***"],
  [/xox[baprs]-[A-Za-z0-9\-]{8,}/g, "xox-***"],
  [/xapp-[A-Za-z0-9\-]{8,}/g, "xapp-***"],
  [/\b\d{6,12}:[A-Za-z0-9_\-]{20,}\b/g, "telegram-***"],
  [/(bearer\s+)[A-Za-z0-9._\-]+/gi, "$1***"],
  [/(api[_-]?key|token|password|passwd|secret|authorization|cookie)(["']?\s*[:=]\s*["']?)[^"',\s&}]+/gi, "$1$2***"]
];

export function containsInternalPromptText(value: unknown) {
  const text = String(value ?? "");
  return INTERNAL_PROMPT_MARKERS.some((marker) => text.includes(marker));
}

export function redactInternalPromptText(value: unknown) {
  const text = String(value ?? "");
  if (RAW_STATUS_ZERO_PATTERN.test(text)) return NETWORK_ERROR_REPLACEMENT;
  const networkRedacted = text.replace(RAW_NETWORK_ERROR_PATTERN, NETWORK_ERROR_REPLACEMENT).trim();
  if (!containsInternalPromptText(networkRedacted)) return networkRedacted;
  const redacted = networkRedacted
    .replace(EXTERNAL_CAPABILITY_CHAIN_PATTERN, USER_VISIBLE_REPLACEMENT)
    .replace(TOOL_CHAIN_REPEAT_PATTERN, USER_VISIBLE_REPLACEMENT)
    .split(/\r?\n/)
    .filter((line) => !INTERNAL_PROMPT_MARKERS.some((marker) => line.includes(marker)))
    .join("\n")
    .trim();
  return redacted || USER_VISIBLE_REPLACEMENT;
}

function redactToolText(value: unknown, maxChars = 512) {
  let text = String(value ?? "");
  for (const [pattern, replacement] of TOOL_SENSITIVE_TEXT_PATTERNS) {
    text = text.replace(pattern, replacement);
  }
  if (text.length > maxChars) return `${text.slice(0, maxChars)}\n...[redacted ${text.length - maxChars} chars]`;
  return text;
}

export function redactToolDisclosureValue(value: unknown, maxDepth = 5): unknown {
  function visit(item: unknown, depth: number, parentKey = ""): unknown {
    if (TOOL_SENSITIVE_KEY_PATTERN.test(parentKey)) return "[redacted]";
    if (TOOL_CONTENT_KEY_PATTERN.test(parentKey)) return "[redacted-content]";
    if (depth <= 0) return "[redacted-nested]";
    if (item === undefined || item === null) return item;
    if (typeof item === "boolean" || typeof item === "number") return item;
    if (typeof item === "string") {
      const trimmed = item.trim();
      if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
        try {
          return visit(JSON.parse(trimmed) as unknown, depth - 1, parentKey);
        } catch {
          // Fall through to text masking for non-JSON strings.
        }
      }
      return redactToolText(item);
    }
    if (Array.isArray(item)) {
      const safe = item.slice(0, 20).map((child) => visit(child, depth - 1, parentKey));
      if (item.length > 20) safe.push({ redacted_omitted_item_count: item.length - 20 });
      return safe;
    }
    if (typeof item === "object") {
      const safe: Record<string, unknown> = {};
      let omitted = 0;
      for (const [key, child] of Object.entries(item as Record<string, unknown>)) {
        if (Object.keys(safe).length >= 20) {
          omitted += 1;
          continue;
        }
        if (TOOL_SENSITIVE_KEY_PATTERN.test(key)) {
          safe[key] = "[redacted]";
        } else if (TOOL_CONTENT_KEY_PATTERN.test(key)) {
          safe[key] = "[redacted-content]";
        } else {
          safe[key] = visit(child, depth - 1, key);
        }
      }
      if (omitted) safe.redacted_omitted_field_count = omitted;
      return safe;
    }
    return redactToolText(item);
  }

  return visit(value, maxDepth);
}
