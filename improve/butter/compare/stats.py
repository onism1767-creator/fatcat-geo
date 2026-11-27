"""比对用的统计小工具(纯层;随机数自己实现、种子固定,同一份输入永远得到同一个结果)。"""

from __future__ import annotations

import math

MASK = (1 << 64) - 1


class Rng:
    """splitmix64:够用、可重现,不依赖标准库 random(纯层不许 import 它,也免得随 Python 版本变)。"""

    def __init__(self, seed: int):
        self.state = seed & MASK

    def next_u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        return z ^ (z >> 31)

    def randrange(self, n: int) -> int:
        return self.next_u64() % n  # n 很小,取模偏差可以忽略


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def band_pp(sigma: float, tau: float, n: int, z: float) -> float:
    """前后差的波动范围:单题噪声随题数缩小,所有题一起漂的 τ 不缩小。"""
    return z * math.sqrt(sigma * sigma / n + tau * tau)


def band_did(sigma: float, n_t: int, n_c: int, z: float) -> float:
    """差中差的波动范围:一起漂的部分两组相减抵消,只剩两组各自的单题噪声。"""
    return z * math.sqrt(sigma * sigma / n_t + sigma * sigma / n_c)


def _percentile(sorted_xs: list[float], q: float) -> float:
    pos = q * (len(sorted_xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def bootstrap_ci(d_t: list[float], d_c: list[float], draws: int, rng: Rng,
                 level: float = 0.95) -> tuple[tuple[float, float], tuple[float, float] | None]:
    """按题重抽样。返回(前后差的区间, 差中差的区间);没有对照组时第二个是 None。"""
    a = (1 - level) / 2
    means_t, dids = [], []
    for _ in range(draws):
        mt = mean([d_t[rng.randrange(len(d_t))] for _ in d_t])
        means_t.append(mt)
        if d_c:
            dids.append(mt - mean([d_c[rng.randrange(len(d_c))] for _ in d_c]))
    means_t.sort()
    ci_t = (_percentile(means_t, a), _percentile(means_t, 1 - a))
    if not d_c:
        return ci_t, None
    dids.sort()
    return ci_t, (_percentile(dids, a), _percentile(dids, 1 - a))


def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def beta_cdf(x: float, a: float, b: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_ppf(q: float, a: float, b: float) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if beta_cdf(mid, a, b) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def jeffreys(hits: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """单题比例的 Jeffreys 区间(页面显示用;小样本比 Wilson 稳)。0 次命中下限取 0,全中上限取 1。"""
    if n <= 0:
        return (0.0, 1.0)
    a = (1 - level) / 2
    lo = 0.0 if hits == 0 else beta_ppf(a, hits + 0.5, n - hits + 0.5)
    hi = 1.0 if hits == n else beta_ppf(1 - a, hits + 0.5, n - hits + 0.5)
    return (lo, hi)
