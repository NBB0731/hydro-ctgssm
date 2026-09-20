from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path("C:/Users/zhao3/Downloads/wr\u6570\u636e.xlsx")
OUT_JSON = ROOT / "work" / "manuscript" / "gfh_experiment_results.json"
OUT_CSV = ROOT / "work" / "manuscript" / "gfh_experiment_processed.csv"


def mean_sd(values: pd.Series) -> dict[str, float]:
    return {"mean": float(values.mean()), "sd": float(values.std(ddof=1))}


def welch_difference(treated: pd.Series, control: pd.Series) -> dict[str, float]:
    a = treated.to_numpy(dtype=float)
    b = control.to_numpy(dtype=float)
    va = a.var(ddof=1) / len(a)
    vb = b.var(ddof=1) / len(b)
    se = np.sqrt(va + vb)
    df = (va + vb) ** 2 / (va**2 / (len(a) - 1) + vb**2 / (len(b) - 1))
    difference = a.mean() - b.mean()
    q = stats.t.ppf(0.975, df)
    test = stats.ttest_ind(a, b, equal_var=False)
    return {
        "difference": float(difference),
        "ci_low": float(difference - q * se),
        "ci_high": float(difference + q * se),
        "p_value": float(test.pvalue),
        "df": float(df),
    }


def ols_hc3(y: pd.Series, x: np.ndarray, names: list[str]) -> dict[str, dict[str, float]]:
    yv = y.to_numpy(dtype=float)
    xv = np.asarray(x, dtype=float)
    xtx_inv = np.linalg.inv(xv.T @ xv)
    beta = xtx_inv @ xv.T @ yv
    residual = yv - xv @ beta
    leverage = np.sum((xv @ xtx_inv) * xv, axis=1)
    adjusted = residual / (1.0 - leverage)
    covariance = xtx_inv @ (xv.T @ (xv * adjusted[:, None] ** 2)) @ xtx_inv
    se = np.sqrt(np.diag(covariance))
    df = len(yv) - xv.shape[1]
    q = stats.t.ppf(0.975, df)
    p_values = 2.0 * stats.t.sf(np.abs(beta / se), df)
    return {
        name: {
            "estimate": float(beta[i]),
            "se_hc3": float(se[i]),
            "ci_low": float(beta[i] - q * se[i]),
            "ci_high": float(beta[i] + q * se[i]),
            "p_value": float(p_values[i]),
        }
        for i, name in enumerate(names)
    }


def ridge_logo(boundary: pd.DataFrame) -> dict:
    features = ["DIPt0", "DIPt10", "DIPt30", "pH t0", "EC t0", "medium"]
    alphas = np.logspace(-4, 4, 81)
    predictions: list[float] = []
    targets: list[float] = []
    folds: list[dict] = []
    for held_out in ["B1", "B2", "B3", "B4"]:
        train = boundary[boundary["水化学组"] != held_out]
        test = boundary[boundary["水化学组"] == held_out]
        best_score = np.inf
        best_alpha = None
        for alpha in alphas:
            inner_errors: list[float] = []
            for validation_group in train["水化学组"].unique():
                inner_train = train[train["水化学组"] != validation_group]
                inner_valid = train[train["水化学组"] == validation_group]
                model = make_pipeline(StandardScaler(), Ridge(alpha=alpha))
                model.fit(inner_train[features], inner_train["DIPt60"])
                inner_errors.extend(abs(inner_valid["DIPt60"] - model.predict(inner_valid[features])))
            score = float(np.mean(inner_errors))
            if score < best_score:
                best_score = score
                best_alpha = float(alpha)
        model = make_pipeline(StandardScaler(), Ridge(alpha=best_alpha))
        model.fit(train[features], train["DIPt60"])
        predicted = model.predict(test[features])
        fold_mae = mean_absolute_error(test["DIPt60"], predicted)
        baseline_mae = mean_absolute_error(test["DIPt60"], test["DIPt30"])
        folds.append(
            {
                "held_out_group": held_out,
                "selected_alpha": best_alpha,
                "ridge_mae_mg_L": float(fold_mae),
                "last_observation_mae_mg_L": float(baseline_mae),
            }
        )
        predictions.extend(float(v) for v in predicted)
        targets.extend(float(v) for v in test["DIPt60"])
    predictions_array = np.asarray(predictions)
    targets_array = np.asarray(targets)
    return {
        "features": features,
        "folds": folds,
        "ridge_mae_mg_L": float(mean_absolute_error(targets_array, predictions_array)),
        "ridge_rmse_mg_L": float(np.sqrt(np.mean((targets_array - predictions_array) ** 2))),
        "last_observation_mae_mg_L": float(
            mean_absolute_error(boundary["DIPt60"], boundary["DIPt30"])
        ),
    }


def main() -> None:
    data = pd.read_excel(SOURCE).dropna(axis=1, how="all")
    required = {
        "样品编号", "水化学组", "pH设定", "EC", "分组", "DIPt0", "DIPt10",
        "DIPt30", "DIPt60", "pH t0", "pH t60", "EC t0", "EC t60", "Fe60(mg/L)"
    }
    missing_columns = sorted(required - set(data.columns))
    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")
    if data["样品编号"].duplicated().any():
        raise ValueError("Sample identifiers are not unique")

    data["medium"] = (data["分组"] == "GFH").astype(int)
    for time in (10, 30, 60):
        data[f"R{time}"] = 100.0 * (data["DIPt0"] - data[f"DIPt{time}"]) / data["DIPt0"]
    data["pH_drift"] = data["pH t60"] - data["pH t0"]
    data["EC_relative_drift_pct"] = 100.0 * (data["EC t60"] - data["EC t0"]) / data["EC t0"]
    data["pH_centered"] = data["pH设定"] - 7.75
    data["log10_EC_centered"] = np.log10(data["EC"]) - np.log10(np.sqrt(100.0 * 1000.0))

    expected_cells = data.groupby(["水化学组", "分组"]).size()
    if len(data) != 30 or not (expected_cells == 3).all():
        raise ValueError(f"Unexpected design counts: {expected_cells.to_dict()}")

    control = data[data["medium"] == 0]
    gfh = data[data["medium"] == 1]
    boundary = data[data["水化学组"] != "CTR"].copy()
    names = ["intercept", "medium", "pH_centered", "log10_EC_centered", "medium_x_pH", "medium_x_log10_EC"]
    design = np.column_stack(
        [
            np.ones(len(boundary)),
            boundary["medium"],
            boundary["pH_centered"],
            boundary["log10_EC_centered"],
            boundary["medium"] * boundary["pH_centered"],
            boundary["medium"] * boundary["log10_EC_centered"],
        ]
    )

    chemistry = {}
    for group, frame in data.groupby("水化学组", sort=False):
        group_control = frame[frame["medium"] == 0]
        group_gfh = frame[frame["medium"] == 1]
        chemistry[group] = {
            "pH_set": float(frame["pH设定"].iloc[0]),
            "EC_set_uS_cm": float(frame["EC"].iloc[0]),
            "control_C60_mg_P_L": mean_sd(group_control["DIPt60"]),
            "gfh_C60_mg_P_L": mean_sd(group_gfh["DIPt60"]),
            "control_R60_pct": mean_sd(group_control["R60"]),
            "gfh_R60_pct": mean_sd(group_gfh["R60"]),
            "gfh_advantage_R60_percentage_points": welch_difference(group_gfh["R60"], group_control["R60"]),
            "control_Fe60_mg_L": mean_sd(group_control["Fe60(mg/L)"]),
            "gfh_Fe60_mg_L": mean_sd(group_gfh["Fe60(mg/L)"]),
        }

    c60_rsd = data.groupby(["水化学组", "分组"])["DIPt60"].agg(["mean", "std"])
    c60_rsd["rsd_pct"] = 100.0 * c60_rsd["std"] / c60_rsd["mean"].abs()

    results = {
        "source_file": str(SOURCE),
        "analysis_date": "2026-08-06",
        "design": {
            "n_bottles": int(len(data)),
            "n_chemistries": int(data["水化学组"].nunique()),
            "n_media": int(data["分组"].nunique()),
            "replicates_per_cell": 3,
            "complete_case_rows": int(data.dropna(subset=list(required)).shape[0]),
        },
        "data_qc": {
            "initial_DIP_mg_P_L": {
                **mean_sd(data["DIPt0"]),
                "cv_pct": float(100.0 * data["DIPt0"].std(ddof=1) / data["DIPt0"].mean()),
                "min": float(data["DIPt0"].min()),
                "max": float(data["DIPt0"].max()),
            },
            "control_R60_pct": {
                **mean_sd(control["R60"]),
                "min": float(control["R60"].min()),
                "max": float(control["R60"].max()),
            },
            "max_cell_C60_rsd_pct": float(c60_rsd["rsd_pct"].max()),
            "max_abs_pH_drift": float(data["pH_drift"].abs().max()),
            "mean_abs_pH_drift": float(data["pH_drift"].abs().mean()),
            "max_abs_EC_relative_drift_pct": float(data["EC_relative_drift_pct"].abs().max()),
            "mean_abs_EC_relative_drift_pct": float(data["EC_relative_drift_pct"].abs().mean()),
            "unavailable_qc": [
                "separate dose-gate data",
                "GFH product and dry-equivalent dose",
                "calibration curve and residuals",
                "method blanks",
                "analytical duplicates",
                "matrix-spike recoveries",
                "natural-water validation",
            ],
        },
        "overall": {
            "control_C60_mg_P_L": mean_sd(control["DIPt60"]),
            "gfh_C60_mg_P_L": mean_sd(gfh["DIPt60"]),
            "gfh_minus_control_C60_mg_P_L": welch_difference(gfh["DIPt60"], control["DIPt60"]),
            "control_R60_pct": mean_sd(control["R60"]),
            "gfh_R60_pct": mean_sd(gfh["R60"]),
            "gfh_advantage_R60_percentage_points": welch_difference(gfh["R60"], control["R60"]),
            "control_Fe60_mg_L": mean_sd(control["Fe60(mg/L)"]),
            "gfh_Fe60_mg_L": mean_sd(gfh["Fe60(mg/L)"]),
            "gfh_minus_control_Fe60_mg_L": welch_difference(gfh["Fe60(mg/L)"], control["Fe60(mg/L)"]),
        },
        "chemistry_groups": chemistry,
        "boundary_hc3_models": {
            "C60_mg_P_L": ols_hc3(boundary["DIPt60"], design, names),
            "R60_pct": ols_hc3(boundary["R60"], design, names),
        },
        "ridge_logo": ridge_logo(boundary),
    }

    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    data.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(OUT_JSON)
    print(OUT_CSV)


if __name__ == "__main__":
    main()
