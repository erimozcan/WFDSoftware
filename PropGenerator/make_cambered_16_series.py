#!/usr/bin/env python3
"""
Generate low-camber NACA 16-series-derived airfoils from symmetric Selig DAT files.

By default this script reads:
  naca16-006.dat, naca16-009.dat, naca16-012.dat

from the same directory as this script and writes:
  wfd_16-206_tip.dat, wfd_16-209_mid.dat, wfd_16-212_root.dat

into the CamberedAirfoils folder beside this script.

The output preserves Selig ordering: upper surface from trailing edge to leading
edge, then lower surface from leading edge to trailing edge.
"""

import argparse
import math
import os
import re


DEFAULT_N_POINTS = 201

AIRFOILS = [
    {
        "source": "naca16-006.dat",
        "output": "wfd_16-206_tip.dat",
        "name": "WFD 16-206 tip, m=0.014 p=0.60",
        "m": 0.014,
        "p": 0.60,
    },
    {
        "source": "naca16-009.dat",
        "output": "wfd_16-209_mid.dat",
        "name": "WFD 16-209 mid, m=0.018 p=0.55",
        "m": 0.018,
        "p": 0.55,
    },
    {
        "source": "naca16-012.dat",
        "output": "wfd_16-212_root.dat",
        "name": "WFD 16-212 root, m=0.020 p=0.50",
        "m": 0.020,
        "p": 0.50,
    },
]


def read_selig_dat(path):
    with open(path, "r") as f:
        raw = f.read()

    lines = normalize_airfoil_text(raw)
    if len(lines) < 4:
        raise ValueError(f"DAT file is too short: {path}")

    name = None
    pts = []
    for line in lines:
        parts = line.replace(",", " ").split()
        if len(parts) < 2:
            if name is None and line:
                name = line
            continue
        try:
            pts.append((float(parts[0]), float(parts[1])))
        except ValueError:
            if name is None and line:
                name = line
            continue

    if len(pts) < 20:
        raise ValueError(f"Could not read enough coordinate points from: {path}")
    if name is None:
        name = os.path.basename(path)
    return name, pts


def normalize_airfoil_text(raw):
    """
    Return plain text lines from either normal DAT text or TextEdit-created RTF.

    The Airfoils folder currently contains .dat.rtf files. This lightweight RTF
    cleanup is intentionally narrow: it strips common control words and keeps
    visible airfoil names/coordinate lines.
    """
    if not raw.lstrip().startswith("{\\rtf"):
        return [line.strip() for line in raw.splitlines() if line.strip()]

    lines = []
    for line in raw.splitlines():
        text = re.sub(r"\\[a-zA-Z*]+-?\d* ?", "", line)
        text = re.sub(r"\\'[0-9a-fA-F]{2}", "", text)
        text = text.replace("{", "").replace("}", "").replace("\\", "").strip()
        text = text.strip(";")
        if not text:
            continue
        if re.search(r"[A-Za-z]", text) and "NACA" not in text.upper():
            continue
        lines.append(text)
    return lines


def find_source_airfoil(input_dir, source_name):
    candidates = [
        os.path.join(input_dir, source_name),
        os.path.join(input_dir, source_name + ".rtf"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        f"Missing source airfoil: {candidates[0]}\n"
        f"Also checked: {candidates[1]}\n"
        "Place the symmetric NACA 16-series DAT files in --input-dir."
    )


def remove_consecutive_duplicates(pts, tol=1e-12):
    cleaned = []
    for x, y in pts:
        if not cleaned:
            cleaned.append((x, y))
            continue
        px, py = cleaned[-1]
        if abs(x - px) > tol or abs(y - py) > tol:
            cleaned.append((x, y))
    return cleaned


def split_selig_surfaces(pts):
    """
    Split ordered Selig coordinates into upper and lower surfaces.

    Selig files normally start at the trailing edge, travel over the upper
    surface to the leading edge, then return along the lower surface to the
    trailing edge. The leading edge is the minimum-x point in that loop.
    """
    pts = remove_consecutive_duplicates(pts)
    le_idx = min(range(len(pts)), key=lambda i: pts[i][0])
    if le_idx <= 0 or le_idx >= len(pts) - 1:
        raise ValueError("Could not split airfoil: leading edge is at an endpoint.")

    upper_te_to_le = pts[: le_idx + 1]
    lower_le_to_te = pts[le_idx:]

    upper_le_to_te = list(reversed(upper_te_to_le))
    lower_le_to_te = lower_le_to_te

    upper = sorted(upper_le_to_te, key=lambda p: p[0])
    lower = sorted(lower_le_to_te, key=lambda p: p[0])
    return upper, lower


def collapse_duplicate_x(surface):
    collapsed = []
    for x, y in sorted(surface, key=lambda p: p[0]):
        if not collapsed or abs(x - collapsed[-1][0]) > 1e-12:
            collapsed.append([x, y, 1])
        else:
            collapsed[-1][1] += y
            collapsed[-1][2] += 1
    return [(x, y_sum / count) for x, y_sum, count in collapsed]


def interp_surface(surface, x_values):
    surface = collapse_duplicate_x(surface)
    if len(surface) < 2:
        raise ValueError("Surface has too few unique x/c stations.")

    xs = [p[0] for p in surface]
    ys = [p[1] for p in surface]
    result = []
    j = 0

    for x in x_values:
        if x <= xs[0]:
            result.append(ys[0])
            continue
        if x >= xs[-1]:
            result.append(ys[-1])
            continue
        while j < len(xs) - 2 and xs[j + 1] < x:
            j += 1
        x0, x1 = xs[j], xs[j + 1]
        y0, y1 = ys[j], ys[j + 1]
        if abs(x1 - x0) <= 1e-12:
            result.append(0.5 * (y0 + y1))
        else:
            t = (x - x0) / (x1 - x0)
            result.append(y0 + (y1 - y0) * t)
    return result


def cosine_x_stations(n_points):
    if n_points < 3:
        raise ValueError("n_points must be at least 3.")
    # Cosine spacing clusters points near the leading and trailing edges.
    return [0.5 * (1.0 - math.cos(math.pi * i / (n_points - 1))) for i in range(n_points)]


def mean_camber_and_slope(x, m, p):
    if not (0.0 < p < 1.0):
        raise ValueError("p must be between 0 and 1.")
    if x < p:
        yc = (m / (p * p)) * (2.0 * p * x - x * x)
        dyc_dx = (2.0 * m / (p * p)) * (p - x)
    else:
        q = 1.0 - p
        yc = (m / (q * q)) * ((1.0 - 2.0 * p) + 2.0 * p * x - x * x)
        dyc_dx = (2.0 * m / (q * q)) * (p - x)
    return yc, dyc_dx


def camber_airfoil(source_pts, m, p, n_points):
    upper, lower = split_selig_surfaces(source_pts)
    x_values = cosine_x_stations(n_points)

    y_upper_sym = interp_surface(upper, x_values)
    y_lower_sym = interp_surface(lower, x_values)
    half_thickness = [0.5 * (yu - yl) for yu, yl in zip(y_upper_sym, y_lower_sym)]

    upper_out = []
    lower_out = []
    for x, yt in zip(x_values, half_thickness):
        yc, dyc_dx = mean_camber_and_slope(x, m, p)
        theta = math.atan(dyc_dx)

        x_upper = x - yt * math.sin(theta)
        y_upper = yc + yt * math.cos(theta)
        x_lower = x + yt * math.sin(theta)
        y_lower = yc - yt * math.cos(theta)

        upper_out.append((x_upper, y_upper))
        lower_out.append((x_lower, y_lower))

    # Selig order: upper TE->LE, then lower LE->TE. Avoid duplicating the LE.
    return list(reversed(upper_out)) + lower_out[1:]


def write_selig_dat(path, name, pts):
    with open(path, "w") as f:
        f.write(f"{name}\n")
        for x, y in pts:
            f.write(f"{x: .8f} {y: .8f}\n")


def plot_airfoils(generated):
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("matplotlib is required for plotting. Install it or rerun with --no-plot.") from exc

    fig, axes = plt.subplots(len(generated), 1, figsize=(9, 3 * len(generated)), squeeze=False)
    for ax, item in zip(axes[:, 0], generated):
        xs = [p[0] for p in item["pts"]]
        ys = [p[1] for p in item["pts"]]
        ax.plot(xs, ys, "-o", markersize=2, linewidth=1)
        ax.axhline(0.0, color="0.75", linewidth=0.8)
        ax.set_title(item["name"])
        ax.set_xlabel("x/c")
        ax.set_ylabel("y/c")
        ax.axis("equal")
        ax.grid(True, linewidth=0.4, alpha=0.5)
    fig.tight_layout()
    plt.show()


def make_all(input_dir, output_dir, n_points, should_plot):
    generated = []
    os.makedirs(output_dir, exist_ok=True)

    for spec in AIRFOILS:
        source_path = find_source_airfoil(input_dir, spec["source"])
        output_path = os.path.join(output_dir, spec["output"])
        _, source_pts = read_selig_dat(source_path)
        out_pts = camber_airfoil(source_pts, spec["m"], spec["p"], n_points)
        write_selig_dat(output_path, spec["name"], out_pts)
        generated.append({"name": spec["name"], "path": output_path, "pts": out_pts})
        print(f"Wrote {output_path}")

    if should_plot:
        plot_airfoils(generated)


def parse_args():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_airfoil_dir = os.path.join(script_dir, "Airfoils")
    default_output_dir = os.path.join(script_dir, "CamberedAirfoils")
    parser = argparse.ArgumentParser(
        description="Create cambered WFD NACA 16-series Selig DAT airfoils."
    )
    parser.add_argument(
        "--input-dir",
        default=default_airfoil_dir,
        help="Directory containing naca16-006.dat, naca16-009.dat, and naca16-012.dat.",
    )
    parser.add_argument(
        "--output-dir",
        default=default_output_dir,
        help="Directory where generated WFD DAT files will be written.",
    )
    parser.add_argument(
        "--points",
        type=int,
        default=DEFAULT_N_POINTS,
        help="Number of x/c stations per surface before Selig LE de-duplication.",
    )
    parser.add_argument(
        "--no-plot",
        action="store_true",
        help="Write DAT files without opening inspection plots.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    make_all(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        n_points=args.points,
        should_plot=not args.no_plot,
    )


if __name__ == "__main__":
    main()
