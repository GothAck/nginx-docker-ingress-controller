#!python3

from pathlib import Path
import sys

END_COMMON = "\n# END COMMON"


def extract_common(file: Path) -> str:
    content = file.read_text()
    return content[: content.index(END_COMMON)]


def main() -> int:
    files = {
        file: extract_common(file)
        for file in Path(".").iterdir()
        if (
            file.is_file
            and file.stem == "Dockerfile"
            and END_COMMON in file.read_text()
        )
    }

    if 1 == len(set(files.values())):
        print("Files have the same common head")
        return 0

    print("Files have differing common head")
    for file, common in files.items():
        print(f">>>> {file} <<<<")
        print(common)
        print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
