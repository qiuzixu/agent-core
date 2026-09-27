"""启动本地文档站（MkDocs）。"""

import subprocess
import sys


def main() -> None:
    subprocess.run(
        [sys.executable, "-m", "mkdocs", "serve"],
        check=True,
    )


if __name__ == "__main__":
    main()
