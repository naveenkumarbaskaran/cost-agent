"""
CostAnalyzer: parse AWS Cost Explorer CSV exports, group spend by service
and tag, detect anomalies, and compute a waste score.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd


class CostAnalyzer:
    """
    Parse and analyse an AWS Cost Explorer CSV export.

    The CSV format produced by AWS Cost Explorer has columns such as::

        LinkedAccountId, Service, UsageType, Operation,
        UsageStartDate, UsageEndDate, UsageQuantity,
        BlendedCost, UnblendedCost, AmortizedCost,
        ResourceTags/user:Environment, ResourceTags/user:Project, …

    Parameters
    ----------
    path:
        Path to the CSV file.
    """

    _TAG_PREFIX = "resourcetags/user:"
    # Fraction of per-service p50 spend below which a period is considered idle
    _IDLE_THRESHOLD = 0.10
    # Z-score threshold for anomaly detection
    _ANOMALY_ZSCORE = 2.5
    # Services that benefit most from Reserved Instances / Savings Plans
    _RI_CANDIDATE_SERVICES = {
        "amazon ec2",
        "amazon rds",
        "amazon elasticache",
        "amazon elasticsearch service",
        "amazon opensearch service",
        "amazon redshift",
    }

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._df: pd.DataFrame | None = None

    # ── public ────────────────────────────────────────────────────────

    def analyze(self) -> dict[str, Any]:
        """
        Full analysis pipeline.

        Returns
        -------
        dict with keys:
            services          : list of service names present in the data
            total_cost        : float, total unblended cost in USD
            by_service        : dict[service_name, total_cost]
            by_tag            : dict[tag_key, dict[tag_value, total_cost]]
            anomalies         : list of anomaly dicts
            idle_resources    : list of idle-resource dicts
            ri_candidates     : list of RI-candidate dicts
            waste_score       : float 0-10 (higher = more waste)
            period            : dict with start and end date strings
        """
        df = self._load()
        cost_col = self._cost_column(df)

        by_service = self._group_by_service(df, cost_col)
        by_tag = self._group_by_tag(df, cost_col)
        anomalies = self._detect_anomalies(df, cost_col)
        idle = self._detect_idle(df, cost_col)
        ri_candidates = self._ri_candidates(by_service)
        waste_score = self._waste_score(anomalies, idle, ri_candidates, by_service)

        date_col = self._date_column(df)
        period: dict[str, str] = {}
        if date_col:
            period = {
                "start": str(df[date_col].min()),
                "end": str(df[date_col].max()),
            }

        return {
            "services": sorted(by_service.keys()),
            "total_cost": round(float(df[cost_col].sum()), 4),
            "by_service": {k: round(v, 4) for k, v in by_service.items()},
            "by_tag": by_tag,
            "anomalies": anomalies,
            "idle_resources": idle,
            "ri_candidates": ri_candidates,
            "waste_score": waste_score,
            "period": period,
        }

    # ── private helpers ───────────────────────────────────────────────

    def _load(self) -> pd.DataFrame:
        if self._df is not None:
            return self._df
        if not self.path.exists():
            raise FileNotFoundError(f"CSV not found: {self.path}")
        df = pd.read_csv(self.path, low_memory=False)
        # Normalise column names: lowercase, strip whitespace
        df.columns = [c.strip().lower() for c in df.columns]
        # Drop rows that are purely header repetitions (AWS sometimes includes them)
        df = df[df.apply(lambda r: not r.astype(str).str.contains("LinkedAccountId", case=False).any(), axis=1)]
        self._df = df
        return df

    @staticmethod
    def _cost_column(df: pd.DataFrame) -> str:
        """Return the best available cost column name."""
        for candidate in ("unblendedcost", "blendedcost", "amortizedcost"):
            if candidate in df.columns:
                df[candidate] = pd.to_numeric(df[candidate], errors="coerce").fillna(0.0)
                return candidate
        # Fallback: first numeric column whose name contains "cost"
        for col in df.columns:
            if "cost" in col:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
                return col
        raise ValueError("No cost column found in CSV. Expected 'UnblendedCost' or similar.")

    @staticmethod
    def _date_column(df: pd.DataFrame) -> str | None:
        for candidate in ("usagestartdate", "usageenddate", "startdate", "date"):
            if candidate in df.columns:
                return candidate
        return None

    @staticmethod
    def _service_column(df: pd.DataFrame) -> str | None:
        for candidate in ("service", "productname", "product/productname"):
            if candidate in df.columns:
                return candidate
        return None

    def _group_by_service(self, df: pd.DataFrame, cost_col: str) -> dict[str, float]:
        svc_col = self._service_column(df)
        if svc_col is None:
            return {}
        grouped = (
            df.groupby(svc_col)[cost_col]
            .sum()
            .sort_values(ascending=False)
        )
        return {str(k): float(v) for k, v in grouped.items() if v > 0}

    def _group_by_tag(self, df: pd.DataFrame, cost_col: str) -> dict[str, dict[str, float]]:
        tag_cols = [c for c in df.columns if c.startswith(self._TAG_PREFIX)]
        result: dict[str, dict[str, float]] = {}
        for col in tag_cols:
            tag_name = col[len(self._TAG_PREFIX):]
            sub = df[df[col].notna() & (df[col].astype(str).str.strip() != "")]
            if sub.empty:
                continue
            grouped = sub.groupby(col)[cost_col].sum().sort_values(ascending=False)
            result[tag_name] = {str(k): round(float(v), 4) for k, v in grouped.items() if v > 0}
        return result

    def _detect_anomalies(self, df: pd.DataFrame, cost_col: str) -> list[dict[str, Any]]:
        """
        Detect services whose daily spend is unusually high relative to
        their own rolling statistics (z-score > threshold).
        """
        date_col = self._date_column(df)
        svc_col = self._service_column(df)
        if date_col is None or svc_col is None:
            return []

        # Parse dates
        df = df.copy()
        df["_date"] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=["_date"])

        daily = df.groupby([svc_col, "_date"])[cost_col].sum().reset_index()
        daily.columns = ["service", "date", "cost"]

        anomalies: list[dict[str, Any]] = []
        for svc, grp in daily.groupby("service"):
            if len(grp) < 3:
                continue
            mean = grp["cost"].mean()
            std = grp["cost"].std()
            if std == 0:
                continue
            grp = grp.copy()
            grp["zscore"] = (grp["cost"] - mean) / std
            spikes = grp[grp["zscore"] > self._ANOMALY_ZSCORE]
            for _, row in spikes.iterrows():
                anomalies.append(
                    {
                        "service": str(svc),
                        "date": str(row["date"].date()),
                        "cost": round(float(row["cost"]), 4),
                        "mean_daily_cost": round(float(mean), 4),
                        "zscore": round(float(row["zscore"]), 2),
                    }
                )
        return sorted(anomalies, key=lambda x: x["zscore"], reverse=True)

    def _detect_idle(self, df: pd.DataFrame, cost_col: str) -> list[dict[str, Any]]:
        """
        Flag service/date combinations where spend is below
        IDLE_THRESHOLD × median service spend (suggesting idle resources).
        """
        date_col = self._date_column(df)
        svc_col = self._service_column(df)
        if date_col is None or svc_col is None:
            return []

        df = df.copy()
        df["_date"] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=["_date"])

        daily = df.groupby([svc_col, "_date"])[cost_col].sum().reset_index()
        daily.columns = ["service", "date", "cost"]

        idle: list[dict[str, Any]] = []
        for svc, grp in daily.groupby("service"):
            if len(grp) < 3:
                continue
            median = grp["cost"].median()
            threshold = median * self._IDLE_THRESHOLD
            # Only flag if there are days near-zero AND the median is meaningful
            if median < 0.5:
                continue
            near_zero = grp[grp["cost"] < threshold]
            if near_zero.empty:
                continue
            idle.append(
                {
                    "service": str(svc),
                    "idle_days": int(len(near_zero)),
                    "total_days": int(len(grp)),
                    "idle_day_examples": [
                        str(d.date()) for d in near_zero["date"].head(3)
                    ],
                    "median_daily_cost": round(float(median), 4),
                }
            )
        return sorted(idle, key=lambda x: x["idle_days"], reverse=True)

    def _ri_candidates(self, by_service: dict[str, float]) -> list[dict[str, Any]]:
        """Identify services that would benefit from Reserved Instances."""
        candidates = []
        for svc, cost in by_service.items():
            if svc.lower() in self._RI_CANDIDATE_SERVICES and cost > 50:
                # Rough estimate: 30-40% saving with 1-year no-upfront RI
                saving_est = cost * 0.35
                candidates.append(
                    {
                        "service": svc,
                        "current_monthly_cost": round(cost, 2),
                        "estimated_monthly_saving": round(saving_est, 2),
                        "recommendation": "Purchase 1-year No-Upfront Reserved Instance or Savings Plan",
                    }
                )
        return sorted(candidates, key=lambda x: x["estimated_monthly_saving"], reverse=True)

    def _waste_score(
        self,
        anomalies: list[dict],
        idle: list[dict],
        ri_candidates: list[dict],
        by_service: dict[str, float],
    ) -> float:
        """
        Compute a waste score 0-10 (higher = more potential savings).

        Combines: anomaly severity, idle-day fraction, RI coverage gap.
        """
        score = 0.0
        total_cost = sum(by_service.values()) or 1

        # Anomaly contribution (up to 3 points)
        anomaly_cost = sum(a["cost"] - a["mean_daily_cost"] for a in anomalies)
        score += min(3.0, (anomaly_cost / total_cost) * 30)

        # Idle contribution (up to 4 points)
        idle_fraction = sum(i["idle_days"] / max(i["total_days"], 1) for i in idle)
        if idle:
            idle_fraction /= len(idle)
        score += min(4.0, idle_fraction * 8)

        # RI gap contribution (up to 3 points)
        ri_saving = sum(r["estimated_monthly_saving"] for r in ri_candidates)
        score += min(3.0, (ri_saving / total_cost) * 6)

        return round(min(10.0, score), 2)
