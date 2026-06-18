"""init_project.py — scaffold projects/<name>/config.json for a fresh project.

A new project folder usually has only `input/<video>` plus a transcript. Every
pipeline stage needs a `config.json` (video_path, issues_path, frames_root, …),
so the first run otherwise required hand-copying another project's config. This
writes one from the shared template, auto-detecting the single video in input/.

    python init_project.py --project 3-task-20260618
    python init_project.py --project p --video projects/p/input/clip.mov --force
"""

from __future__ import annotations

import argparse
import sys

import config


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", required=True, help="project name under projects/")
    p.add_argument("--video",
                   help="video path (default: the single file in input/)")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing config.json")
    args = p.parse_args(argv)

    # Expected, user-actionable failures (config exists, no/many videos) get a
    # clean one-line message, not a traceback.
    try:
        path, cfg = config.scaffold_config(args.project, video=args.video,
                                           force=args.force)
    except (FileExistsError, FileNotFoundError, ValueError) as e:
        sys.exit(str(e))
    print(f"Wrote {path}")
    print(f"  video_path:  {cfg['video_path']}")
    print(f"  issues_path: {cfg['issues_path']}")
    print(f"  server_port: {cfg['server_port']}")


if __name__ == "__main__":
    main()
