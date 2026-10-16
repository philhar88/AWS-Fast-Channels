import Box from "@cloudscape-design/components/box";
import styles from "./JsonBlock.module.css";

/** Pretty-printed JSON (or the raw text when it is not JSON) with a small label. */
export function JsonBlock({ label, text }: { label: string; text: string }) {
  return (
    <div>
      <Box variant="small" color="text-body-secondary">
        {label}
      </Box>
      <pre className={styles.json}>{pretty(text)}</pre>
    </div>
  );
}

function pretty(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}
