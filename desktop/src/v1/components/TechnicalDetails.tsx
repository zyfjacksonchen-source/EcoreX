interface TechnicalDetailEntry {
  label: string;
  value: string | null | undefined;
}

interface TechnicalDetailsProps {
  entries: TechnicalDetailEntry[];
  summary?: string;
}

export function TechnicalDetails({
  entries,
  summary = "技术详情",
}: TechnicalDetailsProps) {
  const visibleEntries = entries.filter((entry) => String(entry.value ?? "").trim());
  if (!visibleEntries.length) return null;
  return (
    <details className="ex-technical-details">
      <summary>{summary}</summary>
      <dl>
        {visibleEntries.map((entry) => (
          <div key={`${entry.label}:${entry.value}`}>
            <dt>{entry.label}</dt>
            <dd><code>{entry.value}</code></dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
