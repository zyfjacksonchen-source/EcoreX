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

export function containsInternalPromptText(value: unknown) {
  const text = String(value ?? "");
  return INTERNAL_PROMPT_MARKERS.some((marker) => text.includes(marker));
}

export function redactInternalPromptText(value: unknown) {
  const text = String(value ?? "");
  if (!containsInternalPromptText(text)) return text;
  const redacted = text
    .replace(EXTERNAL_CAPABILITY_CHAIN_PATTERN, USER_VISIBLE_REPLACEMENT)
    .replace(TOOL_CHAIN_REPEAT_PATTERN, USER_VISIBLE_REPLACEMENT)
    .split(/\r?\n/)
    .filter((line) => !INTERNAL_PROMPT_MARKERS.some((marker) => line.includes(marker)))
    .join("\n")
    .trim();
  return redacted || USER_VISIBLE_REPLACEMENT;
}
