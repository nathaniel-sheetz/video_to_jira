import re
import sys
from pathlib import Path


def clean_vtt(vtt_path: str | Path) -> Path:
    """Strip cue IDs and <v> tags from a WebVTT file.

    Writes <stem>.txt alongside the source file.
    Returns the output path.
    """
    vtt_path = Path(vtt_path)
    text = vtt_path.read_text(encoding="utf-8")

    raw_blocks = text.split("\n\n")
    cleaned_blocks = []

    for block in raw_blocks:
        block = block.strip()
        if not block or block.startswith("WEBVTT") or block.startswith("NOTE"):
            continue

        lines = block.splitlines()

        # Find the timestamp line
        ts_index = next(
            (i for i, line in enumerate(lines) if " --> " in line), None
        )
        if ts_index is None:
            continue

        timestamp = lines[ts_index]
        text_lines = lines[ts_index + 1:]

        if not text_lines:
            continue

        joined = " ".join(text_lines)
        cleaned_text = re.sub(r"<[^>]+>", "", joined).strip()

        if not cleaned_text:
            continue

        cleaned_blocks.append(f"{timestamp}\n{cleaned_text}")

    out_path = vtt_path.with_suffix(".txt")
    out_path.write_text("\n\n".join(cleaned_blocks), encoding="utf-8")
    return out_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python clean_vtt.py <file.vtt>")
        sys.exit(1)

    out = clean_vtt(sys.argv[1])
    print(out)


if __name__ == "__main__":
    main()
