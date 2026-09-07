"""Build and query a lightweight index for local reference checkouts."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "docs" / "references" / "catalog.json"
INDEX_DIR = ROOT / "docs" / "references" / ".index"

IGNORED_DIRS = {
    ".git",
    ".next",
    ".turbo",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
}
TEXT_EXTENSIONS = {
    ".cjs",
    ".js",
    ".jsx",
    ".mjs",
    ".py",
    ".rs",
    ".ts",
    ".tsx",
}
PROJECT_NAMES = ("openclaw", "pi")
SYMBOL_PATTERN = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?|declare\s+)?"
    r"(?:async\s+)?(?:function|class|interface|type|const|let|var)\s+"
    r"([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def load_catalog() -> dict[str, Any]:
    with CATALOG_PATH.open(encoding="utf-8") as stream:
        return json.load(stream)


def project_roots(catalog: dict[str, Any]) -> Iterator[tuple[str, Path]]:
    projects = catalog.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("catalog.json must contain an object at 'projects'")
    for name, metadata in projects.items():
        if not isinstance(metadata, dict) or not isinstance(metadata.get("path"), str):
            raise ValueError(f"project {name!r} must define a string path")
        yield name, ROOT / metadata["path"]


def iter_files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for current, directories, filenames in os.walk(root):
        directories[:] = sorted(name for name in directories if name not in IGNORED_DIRS)
        for filename in sorted(filenames):
            path = Path(current, filename)
            if path.is_file():
                yield path


def relative_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def language_for(path: Path) -> str:
    return {
        ".cjs": "javascript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".py": "python",
        ".rs": "rust",
        ".ts": "typescript",
        ".tsx": "typescript",
    }.get(path.suffix.lower(), "other")


def python_symbols(path: Path) -> Iterable[dict[str, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            yield {
                "name": node.name,
                "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                "path": relative_path(path),
                "line": str(node.lineno),
            }


def javascript_symbols(path: Path) -> Iterable[dict[str, str]]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for match in SYMBOL_PATTERN.finditer(content):
        line = str(content.count("\n", 0, match.start()) + 1)
        declaration = match.group(0)
        if "class " in declaration:
            kind = "class"
        elif "function " in declaration:
            kind = "function"
        elif "interface " in declaration:
            kind = "interface"
        elif "type " in declaration:
            kind = "type"
        else:
            kind = "variable"
        yield {
            "name": match.group(1),
            "kind": kind,
            "path": relative_path(path),
            "line": line,
        }


def symbols_for(path: Path) -> Iterable[dict[str, str]]:
    language = language_for(path)
    if language == "python":
        return python_symbols(path)
    if language in {"javascript", "typescript"}:
        return javascript_symbols(path)
    return ()


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=True, sort_keys=True))
            stream.write("\n")
            count += 1
    return count


def build_index(selected_project: str | None = None, include_symbols: bool = False) -> None:
    catalog = load_catalog()
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    file_records: list[dict[str, Any]] = []
    symbol_records: list[dict[str, str]] = []
    project_counts: dict[str, int] = {}

    for project, root in project_roots(catalog):
        if selected_project and project != selected_project:
            continue
        count = 0
        for path in iter_files(root):
            file_records.append(
                {
                    "project": project,
                    "path": relative_path(path),
                    "language": language_for(path),
                    "bytes": path.stat().st_size,
                }
            )
            if include_symbols and path.suffix.lower() in TEXT_EXTENSIONS:
                for symbol in symbols_for(path):
                    symbol_records.append({"project": project, **symbol})
            count += 1
        project_counts[project] = count

    file_count = write_jsonl(INDEX_DIR / "files.jsonl", file_records)
    symbol_count = write_jsonl(INDEX_DIR / "symbols.jsonl", symbol_records)
    metadata = {
        "generated_at": datetime.now(UTC).isoformat(),
        "catalog": relative_path(CATALOG_PATH),
        "projects": project_counts,
        "files": file_count,
        "symbols": symbol_count,
    }
    with (INDEX_DIR / "metadata.json").open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(metadata, stream, ensure_ascii=True, indent=2, sort_keys=True)
        stream.write("\n")
    print(f"Indexed {file_count} files and {symbol_count} symbols.")


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Index not found: {path}. Run the build command first.")
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def query_index(project: str | None, text: str | None, symbol: str | None, limit: int) -> None:
    if not any((text, symbol)):
        raise ValueError("query requires --text or --symbol")
    records = read_jsonl(INDEX_DIR / ("symbols.jsonl" if symbol else "files.jsonl"))
    needle = (symbol or text or "").lower()
    shown = 0
    for record in records:
        if project and record.get("project") != project:
            continue
        haystack = record.get("name", "") if symbol else record.get("path", "")
        if needle not in str(haystack).lower():
            continue
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        shown += 1
        if shown >= limit:
            break
    if shown == 0:
        print("No matches.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="build the local reference index")
    build.add_argument("--project", choices=PROJECT_NAMES)
    build.add_argument(
        "--symbols",
        action="store_true",
        help="parse Python and TypeScript/JavaScript symbols; slower on large checkouts",
    )
    query = subparsers.add_parser("query", help="query indexed files or symbols")
    query.add_argument("--project", choices=PROJECT_NAMES)
    query.add_argument("--text", help="case-insensitive substring matched against file paths")
    query.add_argument("--symbol", help="case-insensitive substring matched against symbols")
    query.add_argument("--limit", type=int, default=50)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "build":
            build_index(args.project, args.symbols)
        else:
            if args.limit <= 0:
                raise ValueError("--limit must be positive")
            query_index(args.project, args.text, args.symbol, args.limit)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
