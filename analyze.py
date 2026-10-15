"""
analyze.py - turn an audio file into a DMX curve at 40 fps.
One pure colour at a time (colours never blend), chosen by the TYPE of each hit.

How it works
  1. Finds every hit (onset) in the song.
  2. Drops hits that are too close together, keeping the strongest, so a colour
     stays on long enough to be seen (MIN_DWELL).
  3. Listens to the sound of each hit (its low / mid / high make-up) and groups
     similar-sounding hits together automatically (k-means). Deep, bassy hits
     get the first colour, brighter hits get the later colours.
  4. Each hit switches the light to its group's colour at full strength. The
     colour stays until the next hit; the master dimmer gives the punch and fade.
  5. White is reserved for the strongest hits during the loudest moments.
  6. The master dimmer follows loudness and hit strength and never goes dark.

Each frame has 4 values (0-254): Red, Green, Blue, Master dimmer.

Usage:
  python analyze.py yourfile.mp3 [--plot]   # creates curves/<yourfile>.npy
  python analyze.py --all                   # analyzes all audio files in folder
Output: curves/<song>.npy (shape: frames x 4)
"""
import os
import sys
import numpy as np
import librosa
from sklearn.cluster import KMeans

FPS = 40
SR = 24000               # 24000 / 40 = 600 -> exact hop, no timing drift
HOP = SR // FPS
HEADER = 255
CURVES_DIR_NAME = "curves"

# ---- colours, from the deepest-sounding hit group to the brightest -------
COLORS = [
    ("red",     (254,   0,   0)),
    ("yellow",  (254, 190,   0)),
    ("cyan",    (  0, 254, 254)),
    ("magenta", (254,   0, 200)),
]
WHITE = (254, 254, 254)
N_GROUPS = len(COLORS)

# ---- timing / look knobs -------------------------------------------------
MIN_DWELL = 0.18         # seconds: colours never change faster than this
HIT_SENSITIVITY = 0.07   # lower = detects more hits, higher = only strong ones
TAU = 0.15               # seconds: how fast the brightness punch of a hit fades

WHITE_LEVEL = 0.80       # white is only used when the song is at least this loud (0-1)...
WHITE_PERCENTILE = 75    # ...and only on hits stronger than this percentile of those loud hits
WHITE_ON = True          # False = never use white
WHITE_SECONDS = 0.25     # a white flash lasts this long, then the hit's own colour returns

# Master dimmer (overall brightness) - never dark
MIN_MASTER = 0.40
W_LEVEL = 0.50
W_PULSE = 0.50
LEVEL_ATTACK = 0.50
LEVEL_RELEASE = 0.12
LIFT = 0.6
# --------------------------------------------------------------------------

BANDS = [(30, 150), (150, 500), (500, 2500), (2500, 6000), (6000, 11500)]


def norm(x):
    lo, hi = np.percentile(x, 5), np.percentile(x, 98)
    return np.clip((x - lo) / (hi - lo + 1e-9), 0, 1)


def attack_release(x, attack, release):
    out = np.zeros_like(x, dtype=float)
    prev = 0.0
    for i, v in enumerate(x):
        k = attack if v > prev else release
        prev += (v - prev) * k
        out[i] = prev
    return out


def get_curve_path(audio_path, create_dir=True):
    """Returns the dedicated curve path curves/<song_stem>.npy for an audio file."""
    folder = os.path.dirname(os.path.abspath(audio_path)) or os.getcwd()
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    curves_dir = os.path.join(folder, CURVES_DIR_NAME)
    if create_dir:
        os.makedirs(curves_dir, exist_ok=True)
    return os.path.join(curves_dir, f"{stem}.npy")


def find_curve_for_song(audio_path):
    """Finds existing curve file for a given song, checking multiple locations."""
    if not audio_path:
        return None
    folder = os.path.dirname(os.path.abspath(audio_path)) or os.getcwd()
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    base = os.path.basename(audio_path)
    
    candidates = [
        os.path.join(folder, CURVES_DIR_NAME, f"{stem}.npy"),
        os.path.join(folder, CURVES_DIR_NAME, f"{base}.npy"),
        os.path.join(folder, f"{stem}.curve.npy"),
        os.path.join(folder, f"{stem}_curve.npy"),
        os.path.join(folder, f"{stem}.npy"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def analyze_song(path, output_path=None, show_plot=False, status_cb=None):
    """Analyze a song and save its individual curve.npy file."""
    if status_cb:
        status_cb(f"Loading {os.path.basename(path)}...")
    y, _ = librosa.load(path, sr=SR, mono=True)

    if status_cb:
        status_cb("Computing audio spectrum & onsets...")
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=SR, n_fft=2048)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=HOP)[0]
    onset_env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=HOP)
    n = min(S.shape[1], len(rms), len(onset_env))
    S, rms, onset_env = S[:, :n], rms[:n], onset_env[:n]

    # ---- 1. find hits ----
    if status_cb:
        status_cb("Detecting beat hits...")
    cand = librosa.onset.onset_detect(onset_envelope=onset_env, sr=SR, hop_length=HOP,
                                      delta=HIT_SENSITIVITY, wait=2, units="frames")
    cand = cand[cand < n - 1]

    # ---- 2. keep the strongest hits, no two closer than MIN_DWELL ----
    dwell = int(round(MIN_DWELL * FPS))
    kept = []
    for f in sorted(cand, key=lambda f: -onset_env[f]):
        if all(abs(f - k) >= dwell for k in kept):
            kept.append(f)
    hits = np.array(sorted(kept))
    if len(hits) == 0:
        hits = np.array([0])

    # ---- 3. describe each hit by what it sounds like, then group them ----
    if status_cb:
        status_cb("Clustering hit timbres...")
    feats, cents = [], []
    for f in hits:
        post = S[:, f + 1:min(f + 4, n)].mean(axis=1)
        pre = S[:, max(f - 3, 0):max(f, 1)].mean(axis=1)
        inc = np.maximum(post - pre, 0)
        b = np.array([inc[(freqs >= lo) & (freqs < hi)].sum() for lo, hi in BANDS])
        feats.append(np.log(b + 1e-6))
        cents.append((inc * freqs).sum() / (inc.sum() + 1e-9))
    feats, cents = np.array(feats), np.array(cents)
    feats = feats - np.median(feats, axis=0)          # remove the song's overall bass bias
    feats = feats / (feats.std(axis=0) + 1e-9)

    k = min(N_GROUPS, len(hits))
    labels = KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(feats)
    order = np.argsort([cents[labels == g].mean() for g in range(k)])   # deepest first
    group = np.zeros(len(hits), int)
    for rank, g in enumerate(order):
        group[labels == g] = rank

    # ---- loudness / hit strength ----
    level = attack_release(norm(rms) ** LIFT, LEVEL_ATTACK, LEVEL_RELEASE)
    hit_strength = norm(onset_env[hits])
    loud = level[hits] > WHITE_LEVEL
    white_thr = np.percentile(hit_strength[loud], WHITE_PERCENTILE) if loud.any() else 2.0

    # ---- 4/5. build the colour timeline: one pure colour per frame ----
    if status_cb:
        status_cb("Generating color timeline & master dimmer...")
    names = [c[0] for c in COLORS] + ["white"]
    pal = np.array([c[1] for c in COLORS] + [WHITE], dtype=float)
    pick = np.zeros(n, int)
    pulse = np.zeros(n)
    tau = TAU * FPS

    # before the first hit, show the first hit's colour
    pick[:hits[0]] = group[0]

    for i, f in enumerate(hits):
        end = hits[i + 1] if i + 1 < len(hits) else n
        is_white = WHITE_ON and loud[i] and hit_strength[i] >= white_thr
        c = N_GROUPS if is_white else group[i]
        peak = 0.65 + 0.35 * hit_strength[i]
        t = np.arange(end - f)
        decay = np.exp(-t / tau)
        pick[f:end] = c
        if is_white:
            pick[f + int(WHITE_SECONDS * FPS):end] = group[i]
        pulse[f:end] = peak * decay

    rgb = pal[pick]          # pure, full-strength colour; brightness comes from the master dimmer

    # ---- 6. master dimmer ----
    m = np.clip(W_LEVEL * level + W_PULSE * pulse, 0, 1)
    m = MIN_MASTER + (1 - MIN_MASTER) * m

    curve = np.concatenate([rgb, (m * 254)[:, None]], axis=1)
    curve = np.clip(np.round(curve), 0, 254).astype(np.uint8)   # 255 is the header byte

    # Save to individual curve file
    out_file = output_path or get_curve_path(path)
    np.save(out_file, curve)

    # Also save to curve.npy for backward compatibility with single-file scripts
    try:
        legacy_path = os.path.join(os.path.dirname(os.path.abspath(path)) or ".", "curve.npy")
        np.save(legacy_path, curve)
    except Exception:
        pass

    changes = int((np.diff(pick) != 0).sum())
    report = (f"Saved {out_file} ({n} frames, {n / FPS:.1f} s, {len(hits)} hits, "
              f"{changes} color changes)")
    print(report)

    if status_cb:
        status_cb(f"Ready: {os.path.basename(out_file)}")

    if show_plot:
        import matplotlib.pyplot as plt
        tt = np.arange(n) / FPS
        fig, (a1, a2) = plt.subplots(2, 1, figsize=(12, 6), sharex=True,
                                     gridspec_kw={"height_ratios": [2, 1]})
        a1.plot(tt, curve[:, 3] / 254, "k", lw=1.2, label="master")
        a1.plot(tt, level, "gray", lw=0.8, label="loudness")
        for i, f in enumerate(hits):
            col = pal[N_GROUPS if pick[f] == N_GROUPS else group[i]] / 254
            a1.axvline(f / FPS, color=col, lw=1.0, alpha=0.8)
        a1.legend(loc="upper right")
        strip = np.repeat(curve[None, :, :3] / 254.0, 20, axis=0)
        a2.imshow(strip, aspect="auto", extent=[0, n / FPS, 0, 1])
        a2.set_yticks([])
        a2.set_xlabel("seconds   (bottom bar = the colour the light will show)")
        plt.tight_layout()
        plt.show()

    return curve, out_file


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python analyze.py yourfile.mp3 [--plot]")
        print("  python analyze.py --all")
        sys.exit(1)

    if sys.argv[1] == "--all":
        folder = "."
        audio_exts = (".mp3", ".wav", ".ogg", ".flac", ".m4a")
        files = [f for f in sorted(os.listdir(folder)) if f.lower().endswith(audio_exts)]
        print(f"Found {len(files)} audio files in {os.path.abspath(folder)}:")
        for f in files:
            full_p = os.path.join(folder, f)
            existing = find_curve_for_song(full_p)
            if existing and "--force" not in sys.argv:
                print(f"  [SKIPPED] {f} (curve exists: {existing})")
                continue
            print(f"\n--- Analyzing {f} ---")
            analyze_song(full_p)
        print("\nAll files processed.")
        return

    path = sys.argv[1]
    show_plot = "--plot" in sys.argv
    analyze_song(path, show_plot=show_plot)


if __name__ == "__main__":
    main()