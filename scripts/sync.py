#!/usr/bin/env python3

import subprocess
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    dirs = ["wiki", "sources", "user"]

    print("Starting workspace synchronization...")

    for dirname in dirs:
        target = root / dirname
        if not target.is_dir():
            print(f"Directory '{dirname}' does not exist.")
            repo_url = input(f"Please paste the Git repository URL for '{dirname}': ").strip()
            if repo_url:
                subprocess.run(["git", "clone", repo_url, str(target)], check=True)
            else:
                print(f"Skipping '{dirname}' because no URL was provided.")
        else:
            print(f"Directory '{dirname}' exists. Pulling latest changes...")
            subprocess.run(["git", "pull"], cwd=str(target), check=True)

    print("Synchronization complete!")


if __name__ == "__main__":
    main()
