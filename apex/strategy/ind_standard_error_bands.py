"""
apex.strategy.ind_standard_error_bands
=======================================
Standard Error Bands (Jon Andersen, TASC 1996) — envelopes drawn around a
*linear regression* of price rather than around a moving average. Where Bollinger
Bands measure dispersion of price about its mean, Standard Error Bands measure
the dispersion of price about its fitted regression *trend*, which makes the
bands tighten in clean trends and flare when price stops tracking the line.

For each window of `period` bars we fit an ordinary-least-squares line
y = a + b*x (x = 0..period-1) and read off:
  - the regression value at the END of the window (the "linear regression
    curve" point), and
  - the standard error of estimate,
        SE = sqrt( sum(residual^2) / (period - 2) ),
    the textbook regression standard error (period-2 degrees of freedom).

The classic indicator then SMOOTHS both the regression endpoint and the
standard error by a short SMA (default 3) before forming the bands:
        middle = SMA(reg_endpoint, smooth)
        upper  = middle + num_errors * SMA(SE, smooth)
        lower  = middle - num_errors * SMA(SE, smooth)

CONTRACT (mirrors apex.strategy.indicators exactly):
  - Input: a sequence of values (float or Decimal — coerced to float internally;
    these are comparative trend statistics, not accounting figures, so float is
    correct here per the indicators-layer convention).
  - Output: lists the SAME LENGTH as the input, with None in warmup positions
    where there isn't enough history. NEVER returns garbage for thin windows.
  - Deterministic: same input -> same output, always. No I/O, no randomness,
    no wall-clock.

Standard error needs `period - 2` degrees of freedom, so `period` must be >= 3.
"""

from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def _sma(values: list[Optional[float]], period: int) -> list[Optional[float]]:
    """SMA over a list that may contain None (None windows -> None)."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(window) / period  # type: ignore[arg-type]
    return out


def linear_regression_endpoint(
    data: Sequence, period: int
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """
    Fit an OLS line over each trailing `period`-bar window and return
    (endpoint, standard_error) lists, each the same length as the input.

    ``endpoint[i]`` is the value of the fitted line at the most recent bar of the
    window ending at i (i.e. a + b*(period-1)). ``standard_error[i]`` is the
    regression standard error of estimate over that window.

    Both are None until `period` values are available. `period` must be >= 3 so
    the standard error has positive degrees of freedom (period - 2).
    """
    if period < 3:
        raise ValueError("period must be >= 3 (standard error needs period-2 dof)")
    values = _to_floats(data)
    n = len(values)
    endpoint: list[Optional[float]] = [None] * n
    std_err: list[Optional[float]] = [None] * n
    if n < period:
        return endpoint, std_err

    # x = 0..period-1 is identical for every window, so precompute its stats.
    xs = list(range(period))
    sum_x = sum(xs)
    mean_x = sum_x / period
    # Sxx = sum((x - mean_x)^2), strictly positive for period >= 2.
    sxx = sum((x - mean_x) ** 2 for x in xs)
    last_x = period - 1

    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        mean_y = sum(window) / period
        # Slope b = Sxy / Sxx; intercept a = mean_y - b*mean_x.
        sxy = sum((xs[k] - mean_x) * (window[k] - mean_y) for k in range(period))
        b = sxy / sxx
        a = mean_y - b * mean_x
        endpoint[i] = a + b * last_x
        # Residual sum of squares about the fitted line.
        sse = 0.0
        for k in range(period):
            fitted = a + b * xs[k]
            resid = window[k] - fitted
            sse += resid * resid
        # Standard error of estimate with period-2 degrees of freedom.
        std_err[i] = (sse / (period - 2)) ** 0.5
    return endpoint, std_err


def standard_error_bands(
    data: Sequence,
    period: int = 21,
    smooth: int = 3,
    num_errors: float = 2.0,
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """
    Standard Error Bands. Returns (upper, middle, lower), each the same length
    as the input.

    middle = SMA(linear-regression endpoint, smooth)
    upper  = middle + num_errors * SMA(standard error, smooth)
    lower  = middle - num_errors * SMA(standard error, smooth)

    Set ``smooth=1`` for the raw, unsmoothed regression bands. None is returned
    in every warmup position (until both the regression window and the smoothing
    window are full).
    """
    if smooth <= 0:
        raise ValueError("smooth must be positive")
    if num_errors < 0:
        raise ValueError("num_errors must be non-negative")

    endpoint, std_err = linear_regression_endpoint(data, period)
    middle = _sma(endpoint, smooth)
    se_smoothed = _sma(std_err, smooth)

    n = len(middle)
    upper: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n
    for i in range(n):
        if middle[i] is None or se_smoothed[i] is None:
            continue
        spread = num_errors * se_smoothed[i]  # type: ignore[operator]
        upper[i] = middle[i] + spread  # type: ignore[operator]
        lower[i] = middle[i] - spread  # type: ignore[operator]
    return upper, middle, lower
