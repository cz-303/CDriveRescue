"""Build a source-only ZIP; exclude environment, state, caches and outputs."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED


def main():
    root = Path(__file__).resolve().parent
    destination = root.parent / "CDriveRescue_Backend_20261026.zip"
    files = [*root.glob("*.py"), *root.glob("*.md"), *root.glob("*.txt"),
             root / "build_exe.bat", root / ".gitignore"]
    for folder in ("src", "tests"):
        files.extend(path for path in (root / folder).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts
                     and path.suffix in (".py", ".json", ".qss", ".ico", ".png"))
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            archive.write(path, Path(root.name) / path.relative_to(root))
    print(destination)


if __name__ == "__main__":
    main()
