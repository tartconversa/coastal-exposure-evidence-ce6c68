"""区间删失生存估计与整群自助不确定度。

不假设失效时刻可精确观测：
- 试验室腐蚀只知发生在末次 clear 与首次 corrosion 两次巡检之间，区间 (L, R]；
- 现场腐蚀只知发生在 last_clear_day 与 last_seen_day 之间；
- 未观测到腐蚀的经历是右删失，删失时点 L、右端为无穷。

估计量是非参数 Turnbull 自洽估计（在巡检/可见时点构成的网格上分配概率质量），
不做参数寿命外推，也不做试验小时到现场年限的换算。不确定度用整群自助：
试验室按制造批 resample（同批试样相关），现场按设备 resample，
不把同批/同机多个体当成独立样本。
"""
from __future__ import annotations

import weakref
from dataclasses import dataclass, field

import numpy as np

TOL = 1e-9
MAX_ITER = 10000
_RISK_CACHE = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class Observation:
    """一个个体的区间结局。right 为 None 表示右删失 (left, +inf)。"""

    subject_id: str
    left: float
    right: float | None
    cluster_id: str
    outcome: str
    trace: dict = field(default_factory=dict)


@dataclass(eq=False)
class SurvivalResult:
    n_subjects: int
    n_clusters: int
    n_events: int
    censor_counts: dict
    grid: list
    mass: list
    tail_mass: float
    max_followup: float
    observations: list

    def survival(self, horizon):
        """S(horizon)=P(T > horizon)；超出最大随访时返回 None（不可识别）。

        有限桶质量之和可以小于 1，差额（tail_mass）是落在最大随访之外的
        存活质量：任何有限时点它都仍在存活侧，因此 S=1-F(有限桶累计)。
        """
        if horizon is None or horizon > self.max_followup + 1e-9:
            return None
        failed = sum(p for g, p in zip(self.grid, self.mass)
                     if g <= horizon + 1e-9)
        return max(0.0, 1.0 - failed)

    def risk(self, horizon):
        """F(horizon)=P(T <= horizon)，不可识别时返回 None。"""
        s = self.survival(horizon)
        return None if s is None else 1.0 - s

    def event_intervals(self):
        """可追溯的失效观测区间列表。"""
        return [
            {
                "subject_id": o.subject_id,
                "interval": (o.left, o.right),
                "outcome": o.outcome,
                **o.trace,
            }
            for o in self.observations
            if o.right is not None
        ]

    def censor_intervals(self):
        """可追溯的删失记录（含剔除/停试理由）。"""
        return [
            {
                "subject_id": o.subject_id,
                "censored_at": o.left,
                "outcome": o.outcome,
                **o.trace,
            }
            for o in self.observations
            if o.right is None
        ]


def turnbull(observations, tol=TOL, max_iter=MAX_ITER):
    """Turnbull 自洽算法（指示矩阵向量化）。

    网格桶 = 全部有限端点（末次清晰、首次腐蚀、删失时点）+ 一个开放末端桶
    (tau_max, +inf)。区间失效 (L, R] 覆盖满足 L < g <= R 的有限桶；右删失
    (L, +inf) 覆盖所有 g > L 的有限桶以及末端桶。末端桶保证“在删失时点仍
    存活”的个体对 EM 有贡献，避免把存活质量错误压到早期失效点。
    返回 (有限网格, 有限桶质量, 末端桶质量)；有限桶质量之和可以小于 1。
    """
    finite = sorted({o.left for o in observations} |
                    {o.right for o in observations if o.right is not None})
    if not finite:
        return [], [], 1.0
    grid = np.array(finite, dtype=float)
    n_bucket = len(grid) + 1  # 最后一个是 +inf 末端桶
    mass = np.full(n_bucket, 1.0 / n_bucket)
    lefts = np.array([o.left for o in observations], dtype=float)
    raw_rights = np.array(
        [np.nan if o.right is None else o.right for o in observations], dtype=float
    )
    censored = np.isnan(raw_rights)
    rights_eff = np.where(censored, np.inf, raw_rights)

    finite_inside = (grid[None, :] > lefts[:, None] + 1e-12) & np.where(
        censored[:, None],
        True,  # 右删失覆盖所有大于 L 的有限网格点
        grid[None, :] <= rights_eff[:, None] + 1e-12,
    )
    tail_inside = censored.reshape(-1, 1)  # 仅右删失个体覆盖末端桶
    inside = np.concatenate([finite_inside, tail_inside], axis=1)
    inside_count = inside.sum(axis=1)

    for _ in range(max_iter):
        pool = inside @ mass
        alive = pool > 0.0
        safe_pool = np.where(alive, pool, 1.0)
        contributions = (inside * mass[None, :]) / safe_pool[:, None]
        # pool=0 的退化个体：在自身覆盖桶上均摊，避免除零并保持迭代推进
        fallback = inside * (~alive)[:, None] / \
            np.where(inside_count > 0, inside_count, 1)[:, None]
        weights = contributions[alive].sum(axis=0) + fallback[~alive].sum(axis=0)
        total = weights.sum()
        if total <= 0.0:
            break
        new_mass = weights / total
        if np.max(np.abs(new_mass - mass)) < tol:
            mass = new_mass
            break
        mass = new_mass
    return finite, mass[:-1].tolist(), float(mass[-1])


def fit_survival(observations):
    """拟合并组装 SurvivalResult；无个体时返回 None。"""
    if not observations:
        return None
    grid, mass, tail_mass = turnbull(observations)
    max_followup = max(
        o.right if o.right is not None else o.left for o in observations
    )
    censor_counts = {}
    for o in observations:
        if o.right is None:
            censor_counts[o.outcome] = censor_counts.get(o.outcome, 0) + 1
    return SurvivalResult(
        n_subjects=len(observations),
        n_clusters=len({o.cluster_id for o in observations}),
        n_events=sum(1 for o in observations if o.right is not None),
        censor_counts=censor_counts,
        grid=grid,
        mass=mass,
        tail_mass=tail_mass,
        max_followup=max_followup,
        observations=list(observations),
    )


def _resample_clusters(observations, rng):
    clusters = {}
    for o in observations:
        clusters.setdefault(o.cluster_id, []).append(o)
    ids = sorted(clusters)
    picks = rng.choice(len(ids), size=len(ids), replace=True)
    out = []
    for idx in picks:
        out.extend(clusters[ids[idx]])
    return out


def risk_with_ci(result, horizon, replicates=500, seed=7123, level=0.90):
    """给定时点风险的点估计与整群自助单侧上分位数。

    返回 dict；horizon 超出最大随访或样本量不足时 status='unidentifiable'。
    零失效单元的自助上界会退化为 0，此时改用单组 Clopper-Pearson 型保守上界
    1-(1-level)^(1/n)，明确标注来源，不把“没看到”说成“风险为零”。
    """
    base = result.risk(horizon) if result is not None else None
    if result is None or base is None:
        return {
            "horizon": horizon,
            "status": "unidentifiable",
            "reason": "horizon_beyond_followup" if result is not None else "empty_cell",
            "point": None,
            "upper": None,
            "lower": None,
            "replicates": replicates,
            "level": level,
        }

    rng = np.random.default_rng(seed)
    cache = _RISK_CACHE.setdefault(result, {})
    cache_key = (round(float(horizon), 9), replicates, seed, level)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    boots = []
    for _ in range(replicates):
        sample = _resample_clusters(result.observations, rng)
        grid, mass, tail_mass = turnbull(sample)
        failed = sum(p for g, p in zip(grid, mass) if g <= horizon + 1e-9)
        # 与 SurvivalResult.survival 同口径：末端桶质量不计入有限时点风险
        boots.append(max(0.0, failed))
    boots = np.asarray(boots)
    q_hi = float(np.quantile(boots, level))
    q_lo = float(np.quantile(boots, 1.0 - level))
    if q_hi <= 0.0 and result.n_events == 0:
        q_hi = 1.0 - (1.0 - level) ** (1.0 / result.n_subjects)
        upper_source = "zero_event_conservative"
    else:
        upper_source = "cluster_bootstrap_quantile"
    out = {
        "horizon": horizon,
        "status": "estimated",
        "point": float(base),
        "upper": float(q_hi),
        "lower": float(q_lo),
        "replicates": replicates,
        "level": level,
        "upper_source": upper_source,
        "max_followup": result.max_followup,
    }
    cache[cache_key] = out
    return out
