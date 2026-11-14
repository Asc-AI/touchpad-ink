"""Render a recorded touchpad session to a PNG and print basic stats."""

import argparse
import csv
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

X_MAX = 1332
Y_MAX = 828


def load_frames(path):
    frames = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                frames.append(json.loads(line))
    return frames


def build_strokes(frames, max_gap=0.15):
    strokes = []
    active = {}
    for fr in frames:
        t = fr["t"]
        if fr.get("count", 0) == 0:
            active.clear()
            continue
        present = set()
        for c in fr.get("contacts", []):
            if not (c.get("tip") and c.get("inrange")):
                continue
            cid = c["id"]
            present.add(cid)
            s = active.get(cid)
            if s is None or (s["points"] and t - s["points"][-1][0] > max_gap):
                s = {"contact_id": cid, "points": []}
                strokes.append(s)
                active[cid] = s
            s["points"].append((t, c["x"], c["y"]))
        for cid in list(active):
            if cid not in present:
                del active[cid]
    return [s for s in strokes if len(s["points"]) >= 2]


def group_episodes(strokes, gap=1.0):
    groups = []
    cur = []
    last_end = None
    for s in strokes:
        start = s["points"][0][0]
        end = s["points"][-1][0]
        if last_end is not None and start - last_end > gap:
            groups.append(cur)
            cur = []
        cur.append(s)
        last_end = end
    if cur:
        groups.append(cur)
    return groups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl")
    ap.add_argument("--png", default="")
    ap.add_argument("--max-gap", type=float, default=0.15)
    ap.add_argument("--episodes", action="store_true",
                    help="also render one panel per writing episode")
    ap.add_argument("--episode-gap", type=float, default=1.0)
    ap.add_argument("--write-csv", action="store_true",
                    help="rewrite the derived strokes CSV using in-range segmentation")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    frames = load_frames(args.jsonl)
    if not frames:
        print("empty session")
        return 1
    strokes = build_strokes(frames, args.max_gap)
    groups = group_episodes(strokes, args.episode_gap) if strokes else []
    if args.write_csv and strokes:
        csv_path = os.path.splitext(args.jsonl)[0] + ".strokes.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["episode", "stroke_id", "contact_id", "t", "x", "y"])
            for ei, g in enumerate(groups):
                for si, s in enumerate(g):
                    for (t, x, y) in s["points"]:
                        wr.writerow([ei, si, s["contact_id"],
                                     f"{t:.6f}", x, y])
        print("rewrote:", csv_path)
    all_pts = [p for s in strokes for p in s["points"]]
    xs = [p[1] for p in all_pts]
    ys = [p[2] for p in all_pts]
    duration = frames[-1]["t"] - frames[0]["t"]
    print("frames=%d duration=%.2fs approx_hz=%.1f strokes=%d points=%d"
          % (len(frames), duration, len(frames) / duration if duration else 0,
             len(strokes), len(all_pts)))
    if all_pts:
        print("x=%d..%d y=%d..%d" % (min(xs), max(xs), min(ys), max(ys)))

    fig, ax = plt.subplots(figsize=(10, 6.6), dpi=150)
    colors = plt.cm.tab10.colors
    for i, s in enumerate(strokes):
        pts = s["points"]
        px = [p[1] for p in pts]
        py = [p[2] for p in pts]
        ax.plot(px, py, "-", lw=2.0, color=colors[i % len(colors)], alpha=0.9)
        ax.plot(px[0], py[0], "o", ms=6, color=colors[i % len(colors)])
        ax.plot(px[-1], py[-1], "x", ms=8, color=colors[i % len(colors)])
    ax.set_xlim(0, X_MAX)
    ax.set_ylim(Y_MAX, 0)  # touchpad origin is top-left
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, color="#dddddd", lw=0.5)
    ax.set_facecolor("#fafafa")
    ax.set_title("Touchpad session  |  %d strokes, %d points, %.1f s"
                 % (len(strokes), len(all_pts), duration))
    ax.set_xlabel("x (0..%d)" % X_MAX)
    ax.set_ylabel("y (0..%d)" % Y_MAX)
    fig.tight_layout()
    png = args.png or os.path.splitext(args.jsonl)[0] + ".png"
    fig.savefig(png)
    print("saved:", png)

    if args.episodes and strokes:
        import math
        groups = group_episodes(strokes, args.episode_gap)
        print("episodes=%d strokes_per_episode=%s"
              % (len(groups), [len(g) for g in groups]))
        cols = min(3, len(groups))
        rows = math.ceil(len(groups) / cols)
        fig2, axes = plt.subplots(rows, cols, figsize=(4.2 * cols, 4.2 * rows),
                                  dpi=150, squeeze=False)
        flat = [a for row in axes for a in row]
        for idx, ax in enumerate(flat):
            if idx >= len(groups):
                ax.axis("off")
                continue
            g = groups[idx]
            pts = [p for s in g for p in s["points"]]
            ex0, ex1 = min(p[1] for p in pts), max(p[1] for p in pts)
            ey0, ey1 = min(p[2] for p in pts), max(p[2] for p in pts)
            pad_x = max(25, (ex1 - ex0) * 0.12)
            pad_y = max(25, (ey1 - ey0) * 0.12)
            for j, s in enumerate(g):
                sp = s["points"]
                color = colors[j % len(colors)]
                ax.plot([p[1] for p in sp], [p[2] for p in sp], "-",
                        lw=2.0, color=color, alpha=0.95)
                ax.plot(sp[0][1], sp[0][2], "o", ms=5, color=color)
                ax.plot(sp[-1][1], sp[-1][2], "x", ms=7, color=color)
            ax.set_xlim(ex0 - pad_x, ex1 + pad_x)
            ax.set_ylim(ey1 + pad_y, ey0 - pad_y)
            ax.set_aspect("equal", adjustable="box")
            ax.grid(True, color="#e2e2e2", lw=0.5)
            t0 = g[0]["points"][0][0]
            t1 = g[-1]["points"][-1][0]
            ax.set_title("episode %d  |  %d strokes, %.1fs"
                         % (idx, len(g), t1 - t0), fontsize=9)
        fig2.tight_layout()
        ep_png = os.path.splitext(png)[0] + "_episodes.png"
        fig2.savefig(ep_png)
        print("saved:", ep_png)

    if args.show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
