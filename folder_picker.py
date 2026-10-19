import subprocess
import sys


def main() -> int:
    title = {
        "download": "Choose attachment download folder",
        "upload": "Choose attachment upload folder",
    }.get(sys.argv[1] if len(sys.argv) > 1 else "", "Choose folder")

    if sys.platform == "darwin":
        script = (
            f'try\nPOSIX path of (choose folder with prompt "{title}")'
            '\non error number -128\n""\nend try'
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            check=True,
            text=True,
        )
        print(result.stdout.rstrip("\n"))
        return 0

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        folder = filedialog.askdirectory(title=title, mustexist=True)
        if folder:
            print(folder)
    finally:
        root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
