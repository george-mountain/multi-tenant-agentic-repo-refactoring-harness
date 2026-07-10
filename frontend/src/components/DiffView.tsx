function classify(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) return "diff-file";
  if (line.startsWith("@@")) return "diff-hunk";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-del";
  if (
    line.startsWith("diff --git") ||
    line.startsWith("index ") ||
    line.startsWith("new file") ||
    line.startsWith("deleted file") ||
    line.startsWith("rename ") ||
    line.startsWith("similarity ")
  )
    return "diff-file";
  if (line.startsWith("commit ") || line.startsWith("Author:") || line.startsWith("Date:") || line.startsWith("Merge:"))
    return "diff-meta";
  return "diff-ctx";
}

export default function DiffView({ text }: { text: string }) {
  const lines = text.replace(/\n$/, "").split("\n");
  return (
    <pre className="diff" aria-label="diff">
      {lines.map((line, i) => (
        <span key={i} className={`dl ${classify(line)}`}>
          {line || " "}
        </span>
      ))}
    </pre>
  );
}
