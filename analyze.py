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

Usage:  python analyze.py yourfile.mp3 [--plot]
Output: curve.npy  (shape: frames x 4)
"""
import sys
import numpy as np
import librosa
from sklearn.cluster import KMeans

FPS = 40
SR = 24000               # 24000 / 40 = 600 -> exact hop, no timing drift
HOP = SR // FPS

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


def main():
    path = sys.argv[1]
    y, _ = librosa.load(path, sr=SR, mono=True)

    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=SR, n_fft=2048)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=HOP)[0]
    onset_env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=HOP)
    n = min(S.shape[1], len(rms), len(onset_env))
    S, rms, onset_env = S[:, :n], rms[:n], onset_env[:n]

    # ---- 1. find hits ----
    cand = librosa.onset.onset_detect(onset_envelope=onset_env, sr=SR, hop_length=HOP,
                                      delta=HIT_SENSITIVITY, wait=2, units="frames")
    cand = cand[cand < n - 1]
    strength_all = norm(onset_env)

    # ---- 2. keep the strongest hits, no two closer than MIN_DWELL ----
    dwell = int(round(MIN_DWELL * FPS))
    kept = []
    for f in sorted(cand, key=lambda f: -onset_env[f]):
        if all(abs(f - k) >= dwell for k in kept):
            kept.append(f)
    hits = np.array(sorted(kept))

    # ---- 3. describe each hit by what it sounds like, then group them ----
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
    np.save("curve.npy", curve)

    # ---- report ----
    changes = int((np.diff(pick) != 0).sum())
    print(f"{n} frames, {n / FPS:.1f} s, {len(hits)} hits used, "
          f"{changes} colour changes ({changes / (n / FPS):.1f} per second)")
    print("Hit groups (deepest -> brightest) and the share of the song in each colour:")
    for r in range(k):
        sel = group == r
        print(f"  {names[r]:8s} {sel.sum():3d} hits, avg pitch of hit {cents[sel].mean():6.0f} Hz, "
              f"on screen {np.mean(pick == r) * 100:5.1f} %")
    print(f"  {'white':8s} {int(np.sum(pick[hits] == N_GROUPS)):3d} hits, "
          f"on screen {np.mean(pick == N_GROUPS) * 100:5.1f} %")
    print(f"Master dimmer: min {curve[:, 3].min()}, mean {curve[:, 3].mean():.0f}")

    if "--plot" in sys.argv:
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


if __name__ == "__main__":
    main()